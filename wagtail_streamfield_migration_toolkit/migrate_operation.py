import json
import logging
from django.db.models import JSONField, F, Q
from django.db.models.functions import Cast
from django.db.migrations import RunPython
from wagtail.blocks import StreamValue

from wagtail_streamfield_migration_toolkit import utils

logger = logging.getLogger(__name__)


class MigrateStreamData(RunPython):
    """Subclass of RunPython for streamfield data migration operations"""

    def __init__(
        self,
        app_name,
        model_name,
        field_name,
        operations_and_block_paths,
        revisions_from=None,
        chunk_size=1024,
        **kwargs
    ):
        """MigrateStreamData constructor

        Args:
            app_name (str): Name of the app.
            model_name (str): Name of the model.
            field_name (str): Name of the streamfield.
            operations_and_block_paths (:obj:`list` of :obj:`tuple` of (:obj:`operation`, :obj:`str`)):
                List of operations and corresponding block paths to apply.
            revisions_from (:obj:`datetime`, optional): Only revisions created from this date
                onwards will be updated. Passing `None` updates all revisions. Defaults to `None`.
                Note that live and latest revisions will be updated regardless of what value this
                takes.
            chunk_size (:obj:`int`, optional): chunk size for queryset.iterator and bulk_update.
                Defaults to 1024.
            **kwargs: atomic, elidable, hints for superclass RunPython can be given

        Example:
            Renaming a block named `field1` to `block1`::
                MigrateStreamData(
                    app_name="blog",
                    model_name="BlogPage",
                    field_name="content",
                    operations_and_block_paths=[
                        (RenameStreamChildrenOperation(old_name="field1", new_name="block1"), ""),
                    ],
                    revisions_from=datetime.date(2022, 7, 25)
                ),
        """

        self.app_name = app_name
        self.model_name = model_name
        self.field_name = field_name
        self.operations_and_block_paths = operations_and_block_paths
        self.revisions_from = revisions_from
        self.chunk_size = chunk_size

        # TODO add reverse code when needed, will probably need another input (reversible?)
        # super class kwargs - atomic,elidable,hints
        super().__init__(
            code=self.migrate_stream_data_forward,
            reverse_code=lambda *args: None,
            **kwargs
        )

    def migrate_stream_data_forward(self, apps, schema_editor):
        model = apps.get_model(self.app_name, self.model_name)

        Revision = apps.get_model("wagtailcore", "Revision")
        # We check if the models have a field `latest_revision` and make sure it points to the
        # Revision model. This relation is there on models with `RevisionMixin`.
        has_revisions = (
            hasattr(model, "latest_revision")
            and model.latest_revision.field.remote_field.model is Revision
        )
        # Again, check for `live_revision`
        has_live_revisions = (
            hasattr(model, "live_revision")
            and model.live_revision.field.remote_field.model is Revision
        )

        model_queryset = model.objects.annotate(
            raw_content=Cast(F(self.field_name), JSONField())
        ).all()

        updated_model_instances_buffer = []
        if has_revisions:
            # use a set here since latest_revisions and live_revisions can overlap often
            live_and_latest_revision_ids = set()
        for instance in model_queryset.iterator(chunk_size=self.chunk_size):

            # get these revision ids for filtering revisions later
            if has_revisions:
                live_and_latest_revision_ids.add(instance.latest_revision_id)
                if has_live_revisions:
                    live_and_latest_revision_ids.add(instance.live_revision_id)

            raw_data = instance.raw_content
            for operation, block_path_str in self.operations_and_block_paths:
                try:
                    raw_data = utils.apply_changes_to_raw_data(
                        raw_data=raw_data,
                        block_path_str=block_path_str,
                        operation=operation,
                        streamfield=getattr(model, self.field_name),
                    )
                    # - TODO add a return value to util to know if changes were made
                    # - TODO save changed only
                except utils.InvalidBlockDefError as e:
                    raise utils.InvalidBlockDefError(instance=instance) from e

            stream_block = getattr(instance, self.field_name).stream_block
            setattr(
                instance,
                self.field_name,
                StreamValue(stream_block, raw_data, is_lazy=True),
            )
            updated_model_instances_buffer.append(instance)

            if len(updated_model_instances_buffer) == self.chunk_size:
                model.objects.bulk_update(
                    updated_model_instances_buffer, [self.field_name]
                )
                updated_model_instances_buffer = []

        if len(updated_model_instances_buffer) > 0:
            # For any remaining chunks
            model.objects.bulk_update(updated_model_instances_buffer, [self.field_name])

        # For models without revisions
        if not has_revisions:
            return

        # TODO support for wagtail 3 ?
        ContentType = apps.get_model("contenttypes", "ContentType")
        contenttype_id = ContentType.objects.get_for_model(model).id

        if self.revisions_from is not None:
            revision_query = Q(
                created_at__gte=self.revisions_from,
                content_type_id=contenttype_id,
            ) | Q(id__in=live_and_latest_revision_ids)
            # we always update latest and live revision if available
        else:
            revision_query = Q(content_type_id=contenttype_id)
        revision_queryset = Revision.objects.filter(revision_query)

        updated_revisions_buffer = []
        for revision in revision_queryset.iterator(chunk_size=self.chunk_size):

            raw_data = json.loads(revision.content[self.field_name])
            for operation, block_path_str in self.operations_and_block_paths:
                try:
                    raw_data = utils.apply_changes_to_raw_data(
                        raw_data=raw_data,
                        block_path_str=block_path_str,
                        operation=operation,
                        streamfield=getattr(model, self.field_name),
                    )
                except utils.InvalidBlockDefError as e:
                    # TODO this check might be a problem with wagtail 3.0
                    if revision.id not in live_and_latest_revision_ids:
                        logger.exception(
                            utils.InvalidBlockDefError(
                                revision=revision, instance=instance
                            )
                        )
                        continue
                    else:
                        raise utils.InvalidBlockDefError(
                            revision=revision, instance=instance
                        ) from e
                # - TODO add a return value to util to know if changes were made
                # - TODO save changed only

            revision.content[self.field_name] = json.dumps(raw_data)
            updated_revisions_buffer.append(revision)

            if len(updated_revisions_buffer) == self.chunk_size:
                Revision.objects.bulk_update(updated_revisions_buffer, ["content"])
                updated_revisions_buffer = []

        if len(updated_revisions_buffer) > 0:
            Revision.objects.bulk_update(updated_revisions_buffer, ["content"])