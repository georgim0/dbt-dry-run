from typing import Callable, Dict, Optional, cast

from dbt_dry_run.exception import SchemaChangeException, UpstreamFailedException
from dbt_dry_run.literals import replace_upstream_sql
from dbt_dry_run.manifest import Node, OnSchemaChange
from dbt_dry_run.models import DryRunResult, DryRunStatus, Table
from dbt_dry_run.node_runner import NodeRunner


def ignore_handler(dry_run_result: DryRunResult, target_table: Table) -> DryRunResult:
    return dry_run_result.replace_table(target_table)


def append_new_columns_handler(
    dry_run_result: DryRunResult, target_table: Table
) -> DryRunResult:
    if dry_run_result.table is None:
        return dry_run_result
    mapped_predicted_table = {
        field.name: field for field in dry_run_result.table.fields
    }
    mapped_target_table = {field.name: field for field in target_table.fields}
    mapped_predicted_table.update(mapped_target_table)
    return dry_run_result.replace_table(
        Table(fields=list(mapped_predicted_table.values()))
    )


def sync_all_columns_handler(
    dry_run_result: DryRunResult, target_table: Table
) -> DryRunResult:
    return dry_run_result


def fail_handler(dry_run_result: DryRunResult, target_table: Table) -> DryRunResult:
    if dry_run_result.table is None:
        return dry_run_result
    predicted_table_field_names = set(
        [field.name for field in dry_run_result.table.fields]
    )
    target_table_field_names = set([field.name for field in target_table.fields])
    added_fields = predicted_table_field_names.difference(target_table_field_names)
    removed_fields = target_table_field_names.difference(predicted_table_field_names)
    schema_changed = added_fields or removed_fields
    table: Optional[Table] = target_table
    status = dry_run_result.status
    exception = dry_run_result.exception
    if schema_changed:
        table = None
        status = DryRunStatus.FAILURE
        msg = (
            f"Incremental model has changed schemas. "
            f"Fields added: {added_fields}, "
            f"Fields removed: {removed_fields}"
        )
        exception = SchemaChangeException(msg)
    return DryRunResult(
        node=dry_run_result.node, table=table, status=status, exception=exception
    )


ON_SCHEMA_CHANGE_TABLE_HANDLER: Dict[
    OnSchemaChange, Callable[[DryRunResult, Table], DryRunResult]
] = {
    OnSchemaChange.IGNORE: ignore_handler,
    OnSchemaChange.APPEND_NEW_COLUMNS: append_new_columns_handler,
    OnSchemaChange.SYNC_ALL_COLUMNS: sync_all_columns_handler,
    OnSchemaChange.FAIL: fail_handler,
}


class ModelRunner(NodeRunner):
    resource_type = "model"

    def run(self, node: Node) -> DryRunResult:
        try:
            run_sql = self._insert_dependant_sql_literals(node)
        except UpstreamFailedException as e:
            return DryRunResult(node, None, DryRunStatus.FAILURE, e)

        if node.config.materialized == "view":
            run_sql = f"CREATE OR REPLACE VIEW `{node.database}`.`{node.db_schema}`.`{node.alias}` AS (\n{run_sql}\n)"
        status, predicted_table, exception = self._sql_runner.query(run_sql)

        result = DryRunResult(node, predicted_table, status, exception)

        if (
            result.status == DryRunStatus.SUCCESS
            and node.config.materialized == "incremental"
        ):
            target_table = self._sql_runner.get_node_schema(node)
            if target_table:
                on_schema_change = node.config.on_schema_change or OnSchemaChange.IGNORE
                handler = ON_SCHEMA_CHANGE_TABLE_HANDLER[on_schema_change]
                result = handler(result, target_table)

        return result

    def _insert_dependant_sql_literals(self, node: Node) -> str:
        if node.depends_on.deep_nodes is not None:
            results = [
                self._results.get_result(n)
                for n in node.depends_on.deep_nodes
                if n in self._results.keys()
            ]
        else:
            raise KeyError(f"deep_nodes have not been created for {node.unique_id}")
        failed_upstreams = [r for r in results if r.status != DryRunStatus.SUCCESS]
        if failed_upstreams:
            msg = f"Can't insert SELECT literals for {node.unique_id} because {[f.node.unique_id for f in failed_upstreams]} failed"
            raise UpstreamFailedException(msg)
        completed_upstreams = [r for r in results if r.table]

        node_new_sql = node.compiled_sql
        for upstream in completed_upstreams:
            node_new_sql = replace_upstream_sql(
                node_new_sql, upstream.node, cast(Table, upstream.table)
            )
        return node_new_sql