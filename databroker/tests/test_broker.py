from __future__ import absolute_import, division, print_function

import tempfile
import os
import glob
import logging
import sys
import string
import time as ttime
import uuid
from datetime import datetime, date, timedelta
import itertools

import pytest
import six
import numpy as np

if sys.version_info >= (3, 0):
    from bluesky.examples import (det, det1, det2, Reader, ReaderWithFileStore,
                                  ReaderWithFSHandler)
    from bluesky.plans import count, pchain, monitor_during_wrapper

logger = logging.getLogger(__name__)

py3 = pytest.mark.skipif(sys.version_info < (3, 0), reason="requires python 3")


@py3
def test_empty_fixture(db):
    "Test that the db pytest fixture works."
    assert len(db()) == 0


@py3
def test_uid_roundtrip(db, RE):
    RE.subscribe('all', db.insert)
    uid, = RE(count([det]))
    h = db[uid]
    assert h['start']['uid'] == uid


@py3
def test_uid_list_multiple_headers(db, RE):
    RE.subscribe('all', db.insert)
    uids = RE(pchain(count([det]), count([det])))
    headers = db[uids]
    assert uids == [h['start']['uid'] for h in headers]


@py3
def test_get_events(db, RE):
    RE.subscribe('all', db.insert)
    uid, = RE(count([det]))
    h = db[uid]
    assert len(list(db.get_events(h))) == 1
    assert len(list(h.stream())) == 1 + 3

    RE.subscribe('all', db.insert)
    uid, = RE(count([det], num=7))
    h = db[uid]
    assert len(list(db.get_events(h))) == 7
    assert len(list(h.stream())) == 7 + 3


@py3
def test_get_events_multiple_headers(db, RE):
    RE.subscribe('all', db.insert)
    headers = db[RE(pchain(count([det]), count([det])))]
    assert len(list(db.get_events(headers))) == 2


@py3
def test_filtering_stream_name(db, RE):

    # one event stream
    RE.subscribe('all', db.insert)
    uid, = RE(count([det], num=7), bc=1)
    h = db[uid]
    assert len(h.descriptors) == 1
    assert list(h.stream_names) == ['primary']
    assert len(list(db.get_events(h, stream_name='primary'))) == 7
    assert len(db.get_table(h, stream_name='primary')) == 7
    assert len(list(db.get_events(h, stream_name='primary',
                                  fields=['det']))) == 7
    assert len(db.get_table(h, stream_name='primary', fields=['det'])) == 7
    assert len(list(h.stream(stream_name='primary'))) == 7 + 3
    assert len(h.table(stream_name='primary')) == 7
    assert len(list(h.stream(stream_name='primary', fields=['det']))) == 7 + 3
    assert len(h.table(stream_name='primary', fields=['det'])) == 7
    assert len(db.get_table(h, stream_name='primary',
                            fields=['det', 'bc'])) == 7

    # two event streams: 'primary' and 'd_monitor'
    d = Reader('d', fields={'d': lambda: 1}, monitor_intervals=[0.5],
               loop=RE.loop)
    uid, = RE(monitor_during_wrapper(count([det], num=7, delay=0.1), [d]))
    h = db[uid]
    assert len(h.descriptors) == 2
    assert set(h.stream_names) == set(['primary', 'd_monitor'])
    assert len(list(db.get_events(h, stream_name='primary'))) == 7
    assert len(list(h.stream(stream_name='primary'))) == 7 + 3

    assert len(db.get_table(h, stream_name='primary')) == 7

    assert len(db.get_table(h)) == 7  # 'primary' by default
    assert len(h.table(stream_name='primary')) == 7
    assert len(h.table()) == 7  # 'primary' by default

    # TODO sort out why the monitor does not fire during the test
    # assert len(list(db.get_events(h))) == 8  # ALL streams by default

    # assert len(list(h.stream())) == 8 + 3  # ALL streams by default
    # assert len(db.get_table(h, stream_name='d_monitor')) == 1
    # assert len(h.table(stream_name='d_monitor')) == 1
    # assert len(list(h.stream(stream_name='d_monitor'))) == 1 + 3
    # assert len(list(db.get_events(h, stream_name='d_monitor'))) == 1


@py3
def test_get_events_filtering_field(db, RE):
    RE.subscribe('all', db.insert)
    uid, = RE(count([det], num=7))
    h = db[uid]
    assert len(list(db.get_events(h, fields=['det']))) == 7
    assert len(list(h.stream(fields=['det']))) == 7 + 3

    with pytest.raises(ValueError):
        list(db.get_events(h, fields=['not_a_field']))

    uids = RE(pchain(count([det1], num=7), count([det2], num=3)))
    headers = db[uids]

    assert len(list(db.get_events(headers, fields=['det1']))) == 7
    assert len(list(db.get_events(headers, fields=['det2']))) == 3


@py3
def test_deprecated_api(db, RE):
    RE.subscribe('all', db.insert)
    uid, = RE(count([det]))
    h = db.find_headers(uid=uid)
    assert list(db.fetch_events(h))


@py3
def test_indexing(db, RE):
    RE.subscribe('all', db.insert)
    uids = []
    for i in range(10):
        uids.extend(RE(count([det])))

    assert uids[-1] == db[-1]['start']['uid']
    assert uids[-2] == db[-2]['start']['uid']
    assert uids[-1:] == [h['start']['uid'] for h in db[-1:]]
    assert uids[-2:] == [h['start']['uid'] for h in reversed(db[-2:])]
    assert uids[-2:-1] == [h['start']['uid'] for h in reversed(db[-2:-1])]
    assert uids[-5:-1] == [h['start']['uid'] for h in reversed(db[-5:-1])]

    with pytest.raises(ValueError):
        # not allowed to slice into unspecified past
        db[:-5]

    with pytest.raises(IndexError):
        # too far back
        db[-11]


@py3
def test_full_text_search(db, RE):
    RE.subscribe('all', db.insert)

    uid, = RE(count([det]), foo='some words')
    RE(count([det]))

    assert len(db()) == 2

    try:
        db('some words')
    except NotImplementedError:
        raise pytest.skip("This mongo-like backend does not support $text.")

    assert len(db('some words')) == 1
    header, = db('some words')
    assert header['start']['uid'] == uid

    # Full text search does *not* apply to keys.
    assert len(db('foo')) == 0

@py3
def test_table_alignment(db, RE):
    # test time shift issue GH9
    RE.subscribe('all', db.insert)
    uid, = RE(count([det]))
    table = db.get_table(db[uid])
    assert table.notnull().all().all()


@py3
def test_scan_id_lookup(db, RE):
    RE.subscribe('all', db.insert)

    RE.md.clear()
    uid1, = RE(count([det]), marked=True)  # scan_id=1

    assert uid1 == db[1]['start']['uid']

    RE.md.clear()
    uid2, = RE(count([det]))  # scan_id=1 again

    # Now we find uid2 for scan_id=1, but we can get the old one by
    # being more specific.
    assert uid2 == db[1]['start']['uid']
    assert uid1 == db(scan_id=1, marked=True)[0]['start']['uid']


@py3
def test_partial_uid_lookup(db, RE):
    RE.subscribe('all', db.insert)

    # Create enough runs that there are two that begin with the same char.
    for _ in range(50):
        RE(count([det]))

    with pytest.raises(ValueError):
        # Some letter will happen to be the first letter of more than one uid.
        for first_letter in string.ascii_lowercase:
            db[first_letter]


@py3
def test_find_by_float_time(db, RE):
    RE.subscribe('all', db.insert)

    before, = RE(count([det]))
    ttime.sleep(0.25)
    t = ttime.time()
    during, = RE(count([det]))
    ttime.sleep(0.25)
    after, = RE(count([det]))

    # Three runs in total were saved.
    assert len(db()) == 3

    # We'll find the one by specifying a time window around its start time.
    header, = db(start_time=t - 0.1, stop_time=t + 0.2)
    assert header['start']['uid'] == during


@py3
def test_find_by_string_time(db, RE):
    RE.subscribe('all', db.insert)

    uid, = RE(count([det]))
    today = date.today()
    tomorrow = date.today() + timedelta(days=1)
    today_str = today.strftime('%Y-%m-%d')
    tomorrow_str = tomorrow.strftime('%Y-%m-%d')
    day_after_tom = date.today() + timedelta(days=2)
    day_after_tom_str = day_after_tom.strftime('%Y-%m-%d')
    assert len(db(start_time=today_str, stop_time=tomorrow_str)) == 1
    assert len(db(start_time=tomorrow_str, stop_time=day_after_tom_str)) == 0


@py3
def test_data_key(db, RE):
    RE.subscribe('all', db.insert)
    RE(count([det1]))
    RE(count([det1, det2]))
    result1 = db(data_key='det1')
    result2 = db(data_key='det2')
    assert len(result1) == 2
    assert len(result2) == 1


@py3
def test_search_for_smoke(db, RE):
    RE.subscribe('all', db.insert)
    for _ in range(5):
        RE(count([det]))
    # smoketest the search with a set
    uid1 = db[-1]['start']['uid'][:8]
    uid2 = db[-2]['start']['uid'][:8]
    uid3 = db[-3]['start']['uid'][:8]
    queries = [
        {-1, -2},
        (-1, -2),
        -1,
        uid1,
        six.text_type(uid1),
        [uid1, [uid2, uid3]],
        [-1, uid1, slice(-5, 0)],
        slice(-5, 0, 2),
        slice(-5, 0),
    ]
    for query in queries:
        db[query]


@py3
def test_alias(db, RE):
    RE.subscribe('all', db.insert)

    uid, = RE(count([det]))
    RE(count([det]))

    # basic usage of alias
    db.alias('foo', uid=uid)
    assert db.foo == db(uid=uid)

    # can't set alias to existing attribute name
    with pytest.raises(ValueError):
        db.alias('get_events', uid=uid)
    with pytest.raises(ValueError):
        db.dynamic_alias('get_events', lambda: {'uid': uid})

    # basic usage of dynamic alias
    db.dynamic_alias('bar', lambda: {'uid': uid})
    assert db.bar == db(uid=uid)

    # normal AttributeError still works
    with pytest.raises(AttributeError):
        db.this_is_not_a_thing


@py3
def test_filters(db, RE):
    RE.subscribe('all', db.insert)
    RE(count([det]), user='Ken')
    dan_uid, = RE(count([det]), user='Dan', purpose='calibration')
    ken_calib_uid, = RE(count([det]), user='Ken', purpose='calibration')

    assert len(db()) == 3
    db.add_filter(user='Dan')
    assert len(db.filters) == 1
    assert len(db()) == 1
    header, = db()
    assert header['start']['uid'] == dan_uid

    db.clear_filters()
    assert len(db.filters) == 0

    assert len(db(purpose='calibration')) == 2
    db.add_filter(user='Ken')
    assert len(db(purpose='calibration')) == 1
    header, = db(purpose='calibration')

    assert header['start']['uid'] == ken_calib_uid


@py3
@pytest.mark.parametrize(
    'key',
    [slice(1, None, None), # raise because trying to slice by scan id
     slice(-1, 2, None),  # raise because slice stop value is > 0
     slice(None, None, None),  # raise because slice has slice.start == None
     4500,  # raise on not finding a header by a scan id
     str(uuid.uuid4()),  # raise on not finding a header by uuid
     ])
def test_raise_conditions(key, db, RE):
    RE.subscribe('all', db.insert)
    for _ in range(5):
        RE(count([det]))

    with pytest.raises(ValueError):
        db[key]


@py3
def test_stream(db, RE):
    _stream('restream', db, RE)
    _stream('stream', db, RE)  # old name


def _stream(method_name, db, RE):
    RE.subscribe('all', db.insert)
    uid = RE(count([det]), owner='Dan')
    s = getattr(db, method_name)(db[uid])
    name, doc = next(s)
    assert name == 'start'
    assert 'owner' in doc
    name, doc = next(s)
    assert name == 'descriptor'
    assert 'data_keys' in doc
    last_item  = 'event', {'data'}  # fake Event to prime loop
    for item in s:
        name, doc = last_item
        assert name == 'event'
        assert 'data' in doc  # Event
        last_item = item
    name, doc = last_item
    assert name == 'stop'
    assert 'exit_status' in doc # Stop


@py3
def test_process(db, RE):
    uid = RE.subscribe('all', db.insert)
    uid = RE(count([det]))
    c = itertools.count()
    def f(name, doc):
        next(c)

    db.process(db[uid], f)
    assert next(c) == len(list(db.restream(db[uid])))


@py3
def test_get_fields(db, RE):
    RE.subscribe('all', db.insert)
    uid, = RE(count([det1, det2]))
    actual = db.get_fields(db[uid])
    expected = set(['det1', 'det2'])
    assert actual == expected
    actual = db[uid].fields()
    assert actual == expected
    actual = db[uid].fields('primary')
    assert actual == expected


@py3
def test_configuration(db, RE):
    det_with_conf = Reader('det_with_conf', {'a': lambda: 1, 'b': lambda: 2})
    RE.subscribe('all', db.insert)
    uid, = RE(count([det_with_conf]), c=3)
    h = db[uid]

    # check that config is not included by default
    ev = next(db.get_events(h))
    assert set(ev['data'].keys()) == set(['a','b'])

    # find config in descriptor['configuration']
    ev = next(db.get_events(h, fields=['a', 'b']))
    assert 'b' in ev['data']
    assert ev['data']['b'] == 2
    assert 'b' in ev['timestamps']

    # find config in start doc
    ev = next(db.get_events(h, fields=['a', 'c']))
    assert 'c' in ev['data']
    assert ev['data']['c'] == 3
    assert 'c' in ev['timestamps']

    # find config in stop doc
    ev = next(db.get_events(h, fields=['a', 'exit_status']))
    assert 'exit_status' in ev['data']
    assert ev['data']['exit_status'] == 'success'
    assert 'exit_status' in ev['timestamps']


@py3
def test_handler_options(db, RE):
    datum_id = str(uuid.uuid4())
    datum_id2 = str(uuid.uuid4())
    desc_uid = str(uuid.uuid4())
    event_uid = str(uuid.uuid4())
    event_uid2 = str(uuid.uuid4())

    # Side-band resource and datum documents.
    res = db.fs.insert_resource('foo', '', {'x': 1})
    db.fs.insert_datum(res, datum_id, {'y': 2})

    res2 = db.fs.insert_resource('foo', '', {'x': 1})
    db.fs.insert_datum(res2, datum_id2, {'y': 2})

    # Generate a normal run.
    RE.subscribe('all', db.insert)
    rs_uid, = RE(count([det]))

    # Side band an extra descriptor and event into this run.
    data_keys = {'image': {'dtype': 'array', 'source': '', 'shape': [5, 5],
                           'external': 'FILESTORE://simulated'}}
    db.mds.insert_descriptor(run_start=rs_uid, data_keys=data_keys,
                             time=ttime.time(), uid=desc_uid,
                             name='injected')
    db.mds.insert_event(descriptor=desc_uid, time=ttime.time(), uid=event_uid,
                        data={'image': datum_id},
                        timestamps={'image': ttime.time()}, seq_num=0)

    db.mds.insert_event(descriptor=desc_uid, time=ttime.time(), uid=event_uid2,
                        data={'image': datum_id2},
                        timestamps={'image': ttime.time()}, seq_num=0)

    h = db[rs_uid]

    # Get unfilled event.
    ev, ev2 = db.get_events(h, fields=['image'])
    assert ev['data']['image'] == datum_id
    assert not ev['filled']['image']

    # Get filled event -- error because no handler is registered.
    with pytest.raises(KeyError):
        ev, ev2 = db.get_events(h, fields=['image'], fill=True)

    # Get filled event -- error because no handler is registered.
    with pytest.raises(KeyError):
        list(db.get_images(h, 'image'))


    class ImageHandler(object):
        RESULT = np.zeros((5, 5))

        def __init__(self, resource_path, **resource_kwargs):
            self._res = resource_kwargs

        def __call__(self, **datum_kwargs):
            return self.RESULT


    class DummyHandler(object):
        def __init__(*args, **kwargs):
            pass

        def __call__(*args, **kwrags):
            return 'dummy'

    # Use a one-off handler registry.
    ev, ev2 = db.get_events(h, fields=['image'], fill=True,
                        handler_registry={'foo': ImageHandler})
    assert ev['data']['image'].shape == ImageHandler.RESULT.shape
    assert ev['filled']['image']

    # Statefully register the handler.
    db.fs.register_handler('foo', ImageHandler)

    ev, ev2 = db.get_events(h, fields=['image'], fill=True)
    assert ev['data']['image'].shape == ImageHandler.RESULT.shape
    assert db.get_images(h, 'image')[0].shape == ImageHandler.RESULT.shape
    assert ev['filled']['image']

    ev, ev2 = db.get_events(h, fields=['image'])
    assert ev is not ev2
    assert ev['filled'] is not ev2['filled']
    assert not ev['filled']['image']
    db.fill_event(ev)  # , inplace=True)
    assert ev['filled']['image']
    assert not ev2['filled']['image']
    ev2 = db.fill_event(ev2, inplace=False)
    assert ev2['filled']['image']

    # Override the stateful registry with a one-off handler.
    # This maps onto the *data key*, not the resource spec.
    ev, ev2 = db.get_events(h, fields=['image'], fill=True,
                        handler_overrides={'image': DummyHandler})
    assert ev['data']['image'] == 'dummy'
    assert ev['filled']['image']

    res = db.get_table(h, fields=['image'], stream_name='injected', fill=True,
                       handler_registry={'foo': DummyHandler})
    assert res['image'].iloc[0] == 'dummy'
    assert ev['filled']['image']

    res = db.get_table(h, fields=['image'], stream_name='injected', fill=True,
                    handler_overrides={'image': DummyHandler})
    assert res['image'].iloc[0] == 'dummy'
    assert ev['filled']['image']

    # Register the DummyHandler statefully so we can test overriding with
    # ImageHandler for the get_images method below.
    db.fs.register_handler('foo', DummyHandler, overwrite=True)

    res = db.get_images(h, 'image', handler_registry={'foo': ImageHandler})
    assert res[0].shape ==  ImageHandler.RESULT.shape

    res = db.get_images(h, 'image', handler_override=ImageHandler)
    assert res[0].shape == ImageHandler.RESULT.shape


@py3
def test_export(broker_factory, RE):
    db1 = broker_factory()
    db2 = broker_factory()
    RE.subscribe('all', db1.mds.insert)

    # test mds only
    uid, = RE(count([det]))
    db1.export(db1[uid], db2)
    assert db2[uid] == db1[uid]
    assert list(db2.get_events(db2[uid])) == list(db1.get_events(db1[uid]))

    # test file copying
    if not hasattr(db1.fs, 'copy_files'):
        raise pytest.skip("This filestore does not implement copy_files.")

    dir1 = tempfile.mkdtemp()
    dir2 = tempfile.mkdtemp()
    detfs = ReaderWithFileStore('detfs', {'image': lambda: np.ones((5, 5))},
                                fs=db1.fs, save_path=dir1)
    uid, = RE(count([detfs]))

    db1.fs.register_handler('RWFS_NPY', ReaderWithFSHandler)
    db2.fs.register_handler('RWFS_NPY', ReaderWithFSHandler)

    (from_path, to_path), = db1.export(db1[uid], db2, new_root=dir2)
    assert os.path.dirname(from_path) == dir1
    assert os.path.dirname(to_path) == dir2
    assert db2[uid] == db1[uid]
    image1, = db1.get_images(db1[uid], 'image')
    image2, = db2.get_images(db2[uid], 'image')


@py3
def test_export_noroot(broker_factory, RE):
    from bluesky.utils import short_uid
    from bluesky.examples import GeneralReaderWithFileStore

    class LocalWriter(GeneralReaderWithFileStore):
        def stage(self):
            self._file_stem = short_uid()
            self._path_stem = os.path.join(self.save_path, self._file_stem)
            self._resource_id = self.fs.insert_resource(
                self.filestore_spec,
                os.path.join(self.save_path, self._file_stem),
                {})

    dir1 = tempfile.mkdtemp()
    db1 = broker_factory()
    db1.fs.register_handler('RWFS_NPY', ReaderWithFSHandler)

    detfs = LocalWriter('detfs', {'image': lambda: np.ones((5, 5))},
                        fs=db1.fs, save_path=dir1, save_ext='npy')

    RE.subscribe('all', db1.mds.insert)
    uid, = RE(count([detfs], num=3))

    db2 = broker_factory()
    db2.fs.register_handler('RWFS_NPY', ReaderWithFSHandler)

    dir2 = tempfile.mkdtemp()
    file_pairs = db1.export(db1[uid], db2, new_root=dir2)
    for from_path, to_path in file_pairs:
        assert os.path.dirname(from_path) == dir1
        assert os.path.dirname(to_path) == os.path.join(dir2, dir1[1:])

    assert db2[uid] == db1[uid]
    image1s = db1.get_images(db1[uid], 'image')
    image2s = db2.get_images(db2[uid], 'image')
    for im1, im2 in zip(image1s, image2s):
        assert np.array_equal(im1, im2)


@py3
def test_export_size_smoke(broker_factory, RE):

    db1 = broker_factory()
    RE.subscribe('all', db1.mds.insert)

    # test file copying
    if not hasattr(db1.fs, 'copy_files'):
        raise pytest.skip("This filestore does not implement copy_files.")

    dir1 = tempfile.mkdtemp()
    detfs = ReaderWithFileStore('detfs', {'image': lambda: np.ones((5, 5))},
                                fs=db1.fs, save_path=dir1)
    uid, = RE(count([detfs]))

    db1.fs.register_handler('RWFS_NPY', ReaderWithFSHandler)
    size = db1.export_size(db1[uid])
    assert size > 0.
