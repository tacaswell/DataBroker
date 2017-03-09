from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import json
import logging
import os
import os.path
import shutil
from contextlib import contextmanager

import warnings
import boltons.cacheutils
import pymongo
import six
from pkg_resources import resource_filename
from pymongo import MongoClient

from . import core
from . import core_v0
from .handlers_base import DuplicateHandler
from .utils import _make_sure_path_exists

_API_MAP = {0: core_v0,
            1: core}


logger = logging.getLogger(__name__)

try:
    from collections import ChainMap as _ChainMap
except ImportError:
    class _ChainMap(object):
        def __init__(self, primary, fallback=None):
            if fallback is None:
                fallback = {}
            self.fallback = fallback
            self.primary = primary

        def __getitem__(self, k):
            try:
                return self.primary[k]
            except KeyError:
                return self.fallback[k]

        def __setitem__(self, k, v):
            self.primary[k] = v

        def __contains__(self, k):
            return k in self.primary or k in self.fallback

        def __delitem__(self, k):
            del self.primary[k]

        def pop(self, k, v):
            return self.primary.pop(k, v)

        @property
        def maps(self):
            return [self.primary, self.fallback]

        @property
        def parents(self):
            return self.fallback

        def new_child(self, m=None):
            if m is None:
                m = {}

            return _ChainMap(m, self)


class FileStoreRO(object):
    '''Base FileStore object that knows how to read the database.

    Parameters
    ----------
    config : dict
       Much have keys {'database', 'collection', 'host'} and may have a 'port'

    handler_reg : dict, optional
       Mapping between spec names and handler classes

    version : int, optional
        schema version of the database.
        Defaults to 1

    root_map : dict, optional
        str -> str mapping to account for temporarily moved/copied/remounted
        files.  Any resources which have a ``root`` in ``root_map``
        will have the resource path updated before being handed to the
        Handler in ``get_spec_handler``

    '''
    KNOWN_SPEC = dict()
    # load the built-in schema
    for spec_name in ['AD_HDF5', 'AD_SPE']:
        tmp_dict = {}
        resource_name = 'json/{}_resource.json'.format(spec_name)
        datum_name = 'json/{}_datum.json'.format(spec_name)
        with open(resource_filename('filestore', resource_name), 'r') as fin:
            tmp_dict['resource'] = json.load(fin)
        with open(resource_filename('filestore', datum_name), 'r') as fin:
            tmp_dict['datum'] = json.load(fin)
        KNOWN_SPEC[spec_name] = tmp_dict

    def set_root_map(self, root_map):
        '''Set the root map

        Parameters
        ----------
        root_map : dict
            str -> str mapping to account for temporarily
            moved/copied/remounted files.  Any resources which have a
            ``root`` in ``root_map`` will have the resource path
            updated before being handed to the Handler in
            ``get_spec_handler``
        '''
        self.root_map = root_map

    @property
    def version(self):
        return self._version

    @version.setter
    def version(self, val):
        if self._api is not None:
            raise RuntimeError("Can not change api version at runtime")
        self._api = _API_MAP[val]
        self._version = val

    @property
    def DatumNotFound(self):
        return self._api.DatumNotFound

    @property
    def DuplicateKeyError(self):
        return self._api.DuplicateKeyError

    def __init__(self, config, handler_reg=None, version=1, root_map=None):
        self.config = config
        self._api = None
        self.version = version

        if handler_reg is None:
            handler_reg = {}
        self.handler_reg = _ChainMap(handler_reg)

        if root_map is None:
            root_map = {}
        self.root_map = root_map

        self._datum_cache = boltons.cacheutils.LRU(max_size=1000000)
        self._handler_cache = boltons.cacheutils.LRU()
        self._resource_cache = boltons.cacheutils.LRU(on_miss=self._r_on_miss)
        self.__db = None
        self.__conn = None
        self.__datum_col = None
        self.__res_col = None
        self.__res_update_col = None
        self.known_spec = dict(self.KNOWN_SPEC)

    def disconnect(self):
        self.__db = None
        self.__conn = None
        self.__datum_col = None
        self.__res_col = None

    def reconfigure(self, config):
        self.disconnect()
        self.config = config

    def _r_on_miss(self, k):
        col = self._resource_col
        return self._api.resource_given_uid(col, k)

    def resource_given_uid(self, uid):
        col = self._resource_col
        return self._api.resource_given_uid(col, uid)

    def retrieve(self, eid):
        return self._api.retrieve(self._datum_col, eid,
                                  self._datum_cache, self.get_spec_handler,
                                  logger)

    # back compat
    get_datum = retrieve

    def register_handler(self, key, handler, overwrite=False):
        if (not overwrite) and (key in self.handler_reg):
            if self.handler_reg[key] is handler:
                return
            raise DuplicateHandler(
                "You are trying to register a second handler "
                "for spec {}, {}".format(key, self))

        self.deregister_handler(key)
        self.handler_reg[key] = handler

    def deregister_handler(self, key):
        handler = self.handler_reg.pop(key, None)
        if handler is not None:
            name = handler.__name__
            for k in list(self._handler_cache):
                if k[1] == name:
                    del self._handler_cache[k]

    @contextmanager
    def handler_context(self, temp_handlers):
        stash = self.handler_reg
        self.handler_reg = self.handler_reg.new_child(temp_handlers)
        try:
            yield self
        finally:
            popped_reg = self.handler_reg.maps[0]
            self.handler_reg = stash
            for handler in popped_reg.values():
                name = handler.__name__
                for k in list(self._handler_cache):
                    if k[1] == name:
                        del self._handler_cache[k]

    @property
    def _db(self):
        if self.__db is None:
            conn = self._connection
            self.__db = conn.get_database(self.config['database'])
            if self.version > 0:
                sentinel = self.__db.get_collection('sentinel')
                versioned_collection = ['resource', 'datum']
                for col_name in versioned_collection:
                    val = sentinel.find_one({'collection': col_name})
                    if val is None:
                        raise RuntimeError('there is no version sentinel for '
                                           'the {} collection'.format(col_name)
                                           )
                    if val['version'] != self.version:
                        raise RuntimeError('DB version {!r} does not match'
                                           'API version of FS {} for the '
                                           '{} collection'.format(
                                               val, self.version, col_name))
        return self.__db

    @property
    def _resource_col(self):
        if self.__res_col is None:
            self.__res_col = self._db.get_collection('resource')
            self.__res_col.create_index('resource_id')

        return self.__res_col

    @property
    def _resource_update_col(self):
        if self.__res_update_col is None:
            self.__res_update_col = self._db.get_collection('resource_update')
            self.__res_update_col.create_index([
                ('resource', pymongo.DESCENDING),
                ('time', pymongo.DESCENDING)
            ])

        return self.__res_update_col

    @property
    def _datum_col(self):
        if self.__datum_col is None:
            self.__datum_col = self._db.get_collection('datum')
            self.__datum_col.create_index('datum_id', unique=True)
            self.__datum_col.create_index('resource')

        return self.__datum_col

    @property
    def _connection(self):
        if self.__conn is None:
            self.__conn = MongoClient(self.config['host'],
                                      self.config.get('port', None))
        return self.__conn

    def get_spec_handler(self, resource):
        """
        Given a document from the base FS collection return
        the proper Handler

        This should get memozied or shoved into a class eventually
        to minimize open/close thrashing.

        Parameters
        ----------
        resource : ObjectId
            ObjectId of a resource document

        Returns
        -------

        handler : callable
            An object that when called with the values in the event
            document returns the externally stored data

        """
        resource = self._resource_cache[resource]

        h_cache = self._handler_cache

        spec = resource['spec']
        handler = self.handler_reg[spec]

        key = (str(resource['uid']), handler.__name__)

        try:
            return h_cache[key]
        except KeyError:
            pass

        kwargs = resource['resource_kwargs']
        rpath = resource['resource_path']
        root = resource.get('root', '')
        root = self.root_map.get(root, root)
        if root:
            rpath = os.path.join(root, rpath)
        ret = handler(rpath, **kwargs)
        h_cache[key] = ret
        return ret

    def get_history(self, resource_uid):
        '''Generator of all updates to the given resource

        Parameters
        ----------
        resource_uid : Document or str
            The resource to get the history of

        Yields
        ------
        update : Document
        '''
        if self.version == 0:
            raise NotImplementedError('No history in v0 schema')

        for doc in self._api.get_resource_history(
                self._resource_update_col, resource_uid):
            yield doc

    def copy_files(self, resource_or_uid, new_root,
                   verify=False, file_rename_hook=None):
        """
        Copy files associated with a resource to a new directory.

        The registered handler must have a `get_file_list` method and the
        process running this method must have read/write access to both the
        source and destination file systems.

        This method does *not* update the filestore database.

        Internally the resource level directory information is stored
        as two parts: the root and the resource_path.  The 'root' is
        the non-semantic component (typically a mount point) and the
        'resource_path' is the 'semantic' part of the file path.  For
        example, it is common to collect data into paths that look like
        ``/mnt/DATA/2016/04/28``.  In this case we could split this as
        ``/mnt/DATA`` as the 'root' and ``2016/04/28`` as the resource_path.



        Parameters
        ----------
        resource_or_uid : Document or str
            The resource to move the files of

        new_root : str
            The new 'root' to copy the files into

        verify : bool, optional (False)
            Verify that the move happened correctly.  This currently
            is not implemented and will raise if ``verify == True``.

        file_rename_hook : callable, optional
            If provided, must be a callable with signature ::

               def hook(file_counter, total_number, old_name, new_name):
                   pass

            This will be run in the inner loop of the file copy step and is
            run inside of an unconditional try/except block.

        See Also
        --------
        `FileStoreMoving.shift_root`
        `FileStoreMoving.change_root`
        """
        if self.version == 0:
            raise NotImplementedError('V0 has no notion of root so can not '
                                      'change it')
        if verify:
            raise NotImplementedError('Verification is not implemented yet')

        def rename_hook_wrapper(hook):
            if hook is None:
                def noop(n, total, old_name, new_name):
                    return
                return noop

            def safe_hook(n, total, old_name, new_name):
                try:
                    hook(n, total, old_name, new_name)
                except:
                    pass
            return safe_hook

        file_rename_hook = rename_hook_wrapper(file_rename_hook)

        # get list of files
        resource = dict(self.resource_given_uid(resource_or_uid))

        datum_gen = self.datum_gen_given_resource(resource)
        datum_kwarg_gen = (datum['datum_kwargs'] for datum in datum_gen)
        file_list = self.get_file_list(resource, datum_kwarg_gen)

        # check that all files share the same root
        old_root = resource['root']
        if not old_root:
            warnings.warn("There is no 'root' in this resource which "
                          "is required to be able to change the root. "
                          "Please use `fs.shift_root` to move some of "
                          "the path from the 'resource_path' to the "
                          "'root'.  For now assuming '/' as root")
            old_root = os.path.sep

        for f in file_list:
            if not f.startswith(old_root):
                raise RuntimeError('something is very wrong, the files '
                                   'do not all share the same root, ABORT')

        # sort out where new files should go
        new_file_list = [os.path.join(new_root,
                                      os.path.relpath(f, old_root))
                         for f in file_list]
        N = len(new_file_list)
        # copy the files to the new location
        for n, (fin, fout) in enumerate(zip(file_list, new_file_list)):
            # copy files
            file_rename_hook(n, N, fin, fout)
            _make_sure_path_exists(os.path.dirname(fout))
            shutil.copy2(fin, fout)

        return zip(file_list, new_file_list)

    def datum_gen_given_resource(self, resource_or_uid):
        """Given resource or resource uid return associated datum documents.
        """
        actual_resource = self.resource_given_uid(resource_or_uid)
        datum_gen = self._api.get_datum_by_res_gen(self._datum_col,
                                                   actual_resource['uid'])
        return datum_gen

    def get_file_list(self, resource_or_uid, datum_kwarg_gen):
        """Given a resource or resource uid and an iterable of datum kwargs,
        return filepaths.


        DO NOT USE FOR COPYING OR MOVING. This is for debugging only.
        See the methods for moving and copying on the FileStore object.
        """
        actual_resource = self.resource_given_uid(resource_or_uid)
        return self._api.get_file_list(actual_resource, datum_kwarg_gen,
                                       self.get_spec_handler)

    def resource_given_eid(self, eid):
        '''Given a datum eid return its Resource document
        '''
        if self.version == 0:
            raise NotImplementedError('V0 has no notion of root so can not '
                                      'change it so no need for this method')

        res = self._api.resource_given_eid(self._datum_col, eid,
                                           self._datum_cache, logger)
        return self._resource_cache[res]


class FileStore(FileStoreRO):
    '''FileStore object that knows how to create new documents.'''
    def insert_resource(self, spec, resource_path, resource_kwargs, root=None):
        '''
         Parameters
         ----------

         spec : str
             spec used to determine what handler to use to open this
             resource.

         resource_path : str or None
             Url to the physical location of this resource

         resource_kwargs : dict, optional
             resource_kwargs name/value pairs of additional kwargs to be
             passed to the handler to open this resource.

         root : str, optional
             The 'root' part of the resource path.


        '''
        if root is None:
            root = ''

        col = self._resource_col

        return self._api.insert_resource(col, spec, resource_path,
                                         resource_kwargs,
                                         self.known_spec,
                                         root=root)

    def insert_datum(self, resource, datum_id, datum_kwargs):
        '''insert a datum for the given resource

        Parameters
        ----------

        resource : Resource or Resource.id
            Resource object

        datum_id : str
            Unique identifier for this datum.  This is the value stored in
            metadatastore and is the value passed to `retrieve` to get
            the data back out.

        datum_kwargs : dict
            dict with any kwargs needed to retrieve this specific
            datum from the resource.

        '''
        col = self._datum_col

        return self._api.insert_datum(col, resource, datum_id, datum_kwargs,
                                      self.known_spec, self._resource_col)

    def bulk_insert_datum(self, resource, datum_ids, datum_kwarg_list):
        col = self._datum_col

        return self._api.bulk_insert_datum(col, resource, datum_ids,
                                           datum_kwarg_list)

    def shift_root(self, resource_or_uid, shift):
        '''Shift directory levels between root and resource_path

        This is useful because the root can be change via `change_root`.

        Parameters
        ----------
        resource_or_uid : Document or str
            The resource to change the root/resource_path allocation
            of absolute path.

        shift : int
            The amount to shift the split.  Positive numbers move more
            levels into the root and negative values move levels into
            the resource_path

        '''
        if self.version == 0:
            raise NotImplementedError('V0 has no notion of root')

        resource = self.resource_given_uid(resource_or_uid)

        def safe_join(inp):
            if not inp:
                return ''
            return os.path.join(*inp)
        actual_resource = self.resource_given_uid(resource)
        if not isinstance(resource, six.string_types):
            if dict(actual_resource) != dict(resource):
                raise RuntimeError('The resource you hold and the resource '
                                   'the data base holds do not match '
                                   'yours: {!r} db: {!r}'.format(
                                       resource, actual_resource))
        resource = dict(actual_resource)
        resource.setdefault('root', '')
        full_path = os.path.join(resource['root'], resource['resource_path'])
        abs_path = full_path and full_path[0] == os.sep
        root = [_ for _ in resource['root'].split(os.sep) if _]
        rpath = [_ for _ in resource['resource_path'].split(os.sep) if _]

        if shift > 0:
            # to the right
            if shift > len(rpath):
                raise RuntimeError('Asked to shift farther to right '
                                   '({}) than there are directories '
                                   'in the resource_path ({})'.format(
                                       shift, len(rpath)))
            new_root = safe_join(root + rpath[:shift])
            new_rpath = safe_join(rpath[shift:])
        else:
            # sometime to the left
            shift = len(root) + shift
            if shift < 0:
                raise RuntimeError('Asked to shift farther to left '
                                   '({}) than there are directories '
                                   'in the root ({})'.format(
                                       shift-len(root), len(root)))
            new_root = safe_join(root[:shift])
            new_rpath = safe_join((root[shift:] + rpath))
        if abs_path:
            new_root = os.sep + new_root

        new = dict(resource)
        new['root'] = new_root
        new['resource_path'] = new_rpath

        update_col = self._resource_update_col
        resource_col = self._resource_col
        return self._api.update_resource(update_col, resource_col,
                                         actual_resource, new,
                                         cmd_kwargs=dict(shift=shift),
                                         cmd='shift_root')


class FileStoreMoving(FileStore):
    '''FileStore object that knows how to move files.'''
    def change_root(self, resource_or_uid, new_root, remove_origin=True,
                    verify=False, file_rename_hook=None):
        '''Change the root directory of a given resource

        The registered handler must have a `get_file_list` method and the
        process running this method must have read/write access to both the
        source and destination file systems.

        Internally the resource level directory information is stored
        as two parts: the root and the resource_path.  The 'root' is
        the non-semantic component (typically a mount point) and the
        'resource_path' is the 'semantic' part of the file path.  For
        example, it is common to collect data into paths that look like
        ``/mnt/DATA/2016/04/28``.  In this case we could split this as
        ``/mnt/DATA`` as the 'root' and ``2016/04/28`` as the resource_path.



        Parameters
        ----------
        resource_or_uid : Document or str
            The resource to move the files of

        new_root : str
            The new 'root' to copy the files into

        remove_origin : bool, optional (True)
            If the source files should be removed

        verify : bool, optional (False)
            Verify that the move happened correctly.  This currently
            is not implemented and will raise if ``verify == True``.

        file_rename_hook : callable, optional
            If provided, must be a callable with signature ::

               def hook(file_counter, total_number, old_name, new_name):
                   pass

            This will be run in the inner loop of the file copy step and is
            run inside of an unconditional try/except block.

        See Also
        --------
        `FileStore.shift_root`


        .. Warning

           This will change documents in your data base, move files
           and possibly delete files.  Be sure you know what you are
           doing.

        '''
        resource = dict(self.resource_given_uid(resource_or_uid))

        file_lists = self.copy_files(resource, new_root, verify,
                                     file_rename_hook)

        # update the database
        new_resource = dict(resource)
        new_resource['root'] = new_root

        update_col = self._resource_update_col
        resource_col = self._resource_col
        ret = self._api.update_resource(update_col, resource_col,
                                        old=resource,
                                        new=new_resource,
                                        cmd_kwargs=dict(
                                            remove_origin=remove_origin,
                                            verify=verify,
                                            new_root=new_root),
                                        cmd='change_root')

        # remove original files
        if remove_origin:
            for f_old, _ in file_lists:
                os.unlink(f_old)

        # nuke caches
        uid = resource['uid']
        self._resource_cache.pop(uid, None)
        for k in list(self._handler_cache):
            if k[0] == uid:
                del self._handler_cache[k]

        return ret
