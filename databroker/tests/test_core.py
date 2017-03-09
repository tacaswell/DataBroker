from __future__ import absolute_import, division, print_function

from databroker.core import Header


def test_header_dict_conformance():
    # TODO update this if / when we add conformance testing to
    # validate attrs in Header
    target = {'start': {'uid': 'start'},
              'stop': {'uid': 'stop', 'start_uid': 'start'}}

    h = Header(None, **target)
    # hack the descriptor lookup/cache mechanism
    target['descriptors'] = [{'uid': 'desc', 'start_uid': 'start'}]
    h._cache['desc'] = [{'uid': 'desc', 'start_uid': 'start'}]

    assert len(h) == len(target)
    assert set(h) == set(target)
    assert set(h.keys()) == set(target.keys())

    for k, v in h.items():
        assert v == target[k]
        assert v == h[k]

    # this is a dumb test
    assert len(list(h.values())) == len(h)

    n, d = h.to_name_dict_pair()
    assert n == 'header'
    assert d == target
