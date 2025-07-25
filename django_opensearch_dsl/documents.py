import sys
import time
from collections import deque
from functools import partial
from typing import Any, Callable, Iterable, Optional, TextIO, Union

import opensearchpy
from django.db import models
from django.db.models import Q, QuerySet
from opensearchpy.helpers import bulk, parallel_bulk
from opensearchpy.helpers.document import Document as DSLDocument

from . import fields
from .apps import DODConfig
from .enums import BulkAction, CommandAction
from .exceptions import ModelFieldNotMappedError
from .search import Search
from .signals import post_index

model_field_class_to_field_class: dict[type[models.Field], type[fields.DODField]] = {
    models.AutoField: fields.IntegerField,
    models.BigAutoField: fields.LongField,
    models.BigIntegerField: fields.LongField,
    models.BooleanField: fields.BooleanField,
    models.CharField: fields.TextField,
    models.DateField: fields.DateField,
    models.DateTimeField: fields.DateField,
    models.DecimalField: fields.DoubleField,
    models.EmailField: fields.TextField,
    models.FileField: fields.FileField,
    models.FilePathField: fields.KeywordField,
    models.FloatField: fields.DoubleField,
    models.ImageField: fields.FileField,
    models.IntegerField: fields.IntegerField,
    models.NullBooleanField: fields.BooleanField,
    models.PositiveBigIntegerField: fields.LongField,
    models.PositiveIntegerField: fields.IntegerField,
    models.PositiveSmallIntegerField: fields.ShortField,
    models.SlugField: fields.KeywordField,
    models.SmallIntegerField: fields.ShortField,
    models.TextField: fields.TextField,
    models.TimeField: fields.LongField,
    models.URLField: fields.TextField,
    models.UUIDField: fields.KeywordField,
}


class Document(DSLDocument):
    """Allow the definition of Opensearch' index using Django `Model`."""

    _prepared_fields: list[tuple[str, fields.DODField, Callable[[models.Model], Any]]] = []

    def __init__(self, related_instance_to_ignore: Any = None, **kwargs: Any) -> None:
        super(Document, self).__init__(**kwargs)
        # related instances to ignore is required to remove the instance
        # from related models on deletion.
        self._related_instance_to_ignore = related_instance_to_ignore
        self._prepared_fields = self.init_prepare()

    @classmethod
    def search(cls, using: str = None, index: str = None) -> opensearchpy.Search:
        """Return a `Search` object parametrized with the index information."""
        return Search(
            using=cls._get_using(using),
            index=cls._default_index(index),
            doc_type=[cls],
            model=cls.django.model,
        )

    def get_queryset(self, filter_: Optional[Q] = None, exclude: Optional[Q] = None, count: int = None) -> QuerySet:
        """Return the queryset that should be indexed by this doc type."""
        qs = self.django.model.objects.all()

        if filter_:
            qs = qs.filter(filter_)
        if exclude:
            qs = qs.exclude(exclude)
        if count is not None:
            qs = qs[:count]

        return qs

    def _eta(self, start: float, done: int, total: int) -> str:  # pragma: no cover
        if done == 0:
            return "~"
        eta = round((time.time() - start) / done * (total - done))
        unit = "secs"
        if eta > 120:
            eta //= 60
            unit = "mins"
        return f"{eta} {unit}"

    def get_indexing_queryset(
        self,
        verbose: bool = False,
        filter_: Optional[Q] = None,
        exclude: Optional[Q] = None,
        count: int = None,
        action: CommandAction = CommandAction.INDEX,
        stdout: TextIO = sys.stdout,
    ) -> Iterable:
        """Divide the queryset into chunks."""
        chunk_size = self.django.queryset_pagination
        qs = self.get_queryset(filter_=filter_, exclude=exclude, count=count)
        qs = qs.order_by("pk") if not qs.query.is_sliced else qs
        total = qs.count()
        model = self.django.model.__name__
        action = action.present_participle.title()

        i = 0
        done = 0
        start = time.time()
        if verbose:
            stdout.write(f"{action} {model}: 0% ({self._eta(start, done, total)})\r")
        while done < total:
            if verbose:
                stdout.write(f"{action} {model}: {round(i / total * 100)}% ({self._eta(start, done, total)})\r")

            for obj in qs[i : i + chunk_size]:
                done += 1
                yield obj

            i = min(i + chunk_size, total)

        if verbose:
            stdout.write(f"{action} {total} {model}: OK          \n")

    def init_prepare(self) -> list[tuple[str, fields.DODField, Callable[[models.Model], Any]]]:
        """Initialise the data model preparers once here.

        Extracts the preparers from the model and generate a list of callables
        to avoid doing that work on every object instance over.
        """
        index_fields: dict[str, fields.DODField] = getattr(self, "_fields", {})
        preparers = []
        fn: Callable[[models.Model], Any]
        for name, field in iter(index_fields.items()):
            if not isinstance(field, fields.DODField):  # pragma: no cover
                continue

            if not field._path:  # noqa
                field._path = [name]

            prep_func = getattr(self, "prepare_%s_with_related" % name, None)
            if prep_func:
                fn = partial(prep_func, related_to_ignore=self._related_instance_to_ignore)
            else:
                prep_func = getattr(self, "prepare_%s" % name, None)
                if prep_func:
                    fn = prep_func
                else:
                    fn = partial(
                        field.get_value_from_instance,
                        field_value_to_ignore=self._related_instance_to_ignore,
                    )

            preparers.append((name, field, fn))

        return preparers

    def prepare(self, instance: models.Model) -> dict[str, Any]:
        """Generate the opensearch's document from `instance` based on defined fields."""
        data = {name: prep_func(instance) for name, field, prep_func in self._prepared_fields}
        return data

    @classmethod
    def to_field(cls, field_name: str, model_field: models.Field) -> fields.DODField:
        """Return the opensearch field instance mapped to the model field class.

        This is a good place to hook into if you have more complex
        model field to OS field logic.
        """
        try:
            return model_field_class_to_field_class[model_field.__class__](attr=field_name)
        except KeyError:  # pragma: no cover
            raise ModelFieldNotMappedError(f"Cannot convert model field {field_name} to an Opensearch field!")

    def bulk(
        self, actions: Iterable[dict[str, Any]], using: str = None, **kwargs: Any
    ) -> Union[tuple[int, int], tuple[int, list]]:
        """Execute given actions in bulk."""
        response = bulk(client=self._get_connection(using), actions=actions, **kwargs)
        # send post index signal
        post_index.send(sender=self.__class__, instance=self, actions=actions, response=response)
        return response

    def parallel_bulk(self, actions: Iterable[dict[str, Any]], using: str = None, **kwargs: Any) -> tuple[int, list]:
        """Parallel version of `bulk`."""
        kwargs.setdefault("chunk_size", self.django.queryset_pagination)
        bulk_actions = parallel_bulk(client=self._get_connection(using), actions=actions, **kwargs)
        # As the `parallel_bulk` is lazy, we need to get it into `deque` to run
        # it instantly.
        # See https://discuss.elastic.co/t/helpers-parallel-bulk-in-python-not-working/39498/2  # noqa
        deque(bulk_actions, maxlen=0)
        # Fake return value to emulate bulk() since we don't have a result yet,
        # the result is currently not used upstream anyway.
        return 1, []

    @classmethod
    def generate_id(cls, object_instance: models.Model) -> Any:
        """Generate the opensearch's _id from a Django `Model` instance.

        The default behavior is to use the Django object's pk (id) as the
        opensearch index id (_id). If needed, this method can be overloaded
        to change this default behavior.
        """
        return object_instance.pk

    def _prepare_action(self, object_instance: models.Model, action: BulkAction) -> dict[str, Any]:
        return {
            "_op_type": action.value,
            "_index": self._index._name,  # noqa
            "_id": self.generate_id(object_instance),
            "_source" if action != "update" else "doc": (self.prepare(object_instance) if action != "delete" else None),
        }

    def _get_actions(self, object_list: Iterable[models.Model], action: BulkAction) -> Iterable[dict[str, Any]]:
        for object_instance in object_list:
            if action == "delete" or self.should_index_object(object_instance):
                yield self._prepare_action(object_instance, action)

    def _bulk(
        self, actions: Iterable[dict[str, Any]], parallel: bool = False, using: str = None, **kwargs: Any
    ) -> Union[tuple[int, int], tuple[int, list]]:
        """Allow switching between normal and parallel bulk operation."""
        if parallel:
            return self.parallel_bulk(actions, using=using, **kwargs)
        return self.bulk(actions, using=using, **kwargs)

    def should_index_object(self, object_instance: models.Model) -> bool:
        """Whether given object should be indexed.

        Overwriting this method and returning a boolean value should determine
        whether the object should be indexed.
        """
        return True

    def update(  # type: ignore[override]  # noqa
        self,
        thing: Union[models.Model, Iterable[models.Model]],
        action: BulkAction,
        refresh: bool = None,
        parallel: bool = None,
        using: str = None,
        **kwargs: Any,
    ) -> Union[tuple[int, int], tuple[int, list]]:
        """Update document in OS for a model, iterable of models or queryset."""
        if refresh is None:
            refresh = getattr(self.Index, "auto_refresh", DODConfig.auto_refresh_enabled())
        if parallel is None:
            parallel = DODConfig.parallel_enabled()

        object_list = [thing] if isinstance(thing, models.Model) else thing

        return self._bulk(
            self._get_actions(object_list, action), parallel=parallel, refresh=refresh, using=using, **kwargs
        )
