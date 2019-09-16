import glob
import json
import os
import pathlib

from ..in_memory import BlueskyInMemoryCatalog
from ..core import tail


def gen(filename):
    """
    A JSONL file generator.

    Parameters
    ----------
    filename: str
        JSONL file to load.
    """
    raise NotImplementedError


class BlueskyJSONLCatalog(BlueskyInMemoryCatalog):
    name = "bluesky-jsonl-catalog"  # noqa

    def __init__(
        self, paths, *, handler_registry=None, root_map=None, query=None, **kwargs
    ):
        """
        This Catalog is backed by a newline-delimited JSON (jsonl) file.

        Each line of the file is expected to be a JSON list with two elements,
        the document name (type) and the document itself. The documents are
        expected to be in chronological order.

        Parameters
        ----------
        paths : list
            list of filepaths
        handler_registry : dict, optional
            Maps each asset spec to a handler class or a string specifying the
            module name and class name, as in (for example)
            ``{'SOME_SPEC': 'module.submodule.class_name'}``.
        root_map : dict, optional
            Maps resource root paths to different paths.
        query : dict, optional
            Mongo query that filters entries' RunStart documents
        **kwargs :
            Additional keyword arguments are passed through to the base class,
            Catalog.
        """
        # Tolerate a single path (as opposed to a list).
        if isinstance(paths, (str, pathlib.Path)):
            paths = [paths]
        self.paths = paths
        self._filename_to_mtime = {}
        super().__init__(
            handler_registry=handler_registry, root_map=root_map, query=query, **kwargs
        )

    def _load(self):
        for path in self.paths:
            for filename in glob.glob(path):
                mtime = os.path.getmtime(filename)
                if mtime == self._filename_to_mtime.get(filename):
                    # This file has not changed since last time we loaded it.
                    continue
                self._filename_to_mtime[filename] = mtime
                with open(filename, "r") as file:
                    for scans in SOMETHING(file):
                        start_doc, stop_doc = SOMETHING()
                        self.upsert(
                            start_doc, stop_doc, gen, (SOMETHING, SOMETHING), {}
                        )

    def search(self, query):
        """
        Return a new Catalog with a subset of the entries in this Catalog.

        Parameters
        ----------
        query : dict
        """
        query = dict(query)
        if self._query:
            query = {"$and": [self._query, query]}
        cat = type(self)(
            paths=self.paths,
            query=query,
            handler_registry=self.filler.handler_registry,
            root_map=self.filler.root_map,
            name="search results",
            getenv=self.getenv,
            getshell=self.getshell,
            auth=self.auth,
            metadata=(self.metadata or {}).copy(),
            storage_options=self.storage_options,
        )
        return cat

    def _get_serializer(self):
        "This is used internally by v1.Broker. It may be removed in future."
        from suitcase.spec import Serializer
        from event_model import RunRouter

        path, *_ = self.paths
        directory = os.path.dirname(path)

        def factory(name, doc):
            serializer = Serializer(directory)
            serializer(name, doc)
            return [serializer], []

        return RunRouter([factory])
