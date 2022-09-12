from django.db import connection
from django.db.migrations import Migration
from django.db.migrations.loader import MigrationLoader


from wagtail_streamfield_migration_toolkit.migrate_operation import MigrateStreamData


class MigrationTestMixin:
    model = None
    default_operation_and_block_path = []

    def apply_migration(
        self,
        revisions_from=None,
        operations_and_block_path=None,
    ):
        migration = Migration(
            "test_migration", "wagtail_streamfield_migration_toolkit_test"
        )
        migration_operation = MigrateStreamData(
            app_name="wagtail_streamfield_migration_toolkit_test",
            model_name=self.model.__name__,
            field_name="content",
            operations_and_block_paths=operations_and_block_path
            or self.default_operation_and_block_path,
            revisions_from=revisions_from,
        )
        migration.operations = [migration_operation]

        loader = MigrationLoader(connection=connection)
        loader.build_graph()
        project_state = loader.project_state()
        schema_editor = connection.schema_editor(atomic=migration.atomic)
        migration.apply(project_state, schema_editor)