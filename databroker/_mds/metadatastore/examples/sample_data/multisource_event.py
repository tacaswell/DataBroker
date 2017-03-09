from __future__ import division
import uuid
import numpy as np
from metadatastore.examples.sample_data import common

# "Magic numbers" for this simulation
start, stop, step, points_per_step = 0, 3, 1, 7
deadband_size = 0.9
num_exposures = 23


@common.example
def run(mds, run_start=None, sleep=0):
    if sleep != 0:
        raise NotImplementedError("A sleep time is not implemented for this "
                                  "example.")
    # Make the data
    ramp = common.stepped_ramp(start, stop, step, points_per_step)
    deadbanded_ramp = common.apply_deadband(ramp, deadband_size)
    rs = np.random.RandomState(5)
    point_det_data = rs.randn(num_exposures)

    # Create Event Descriptors
    data_keys1 = {'point_det': dict(source='PV:ES:PointDet',
                                    dtype='number')}
    data_keys2 = {'Tsam': dict(source='PV:ES:Tsam', dtype='number'),
                  'Troom': dict(source='PV:ES:Troom', dtype='number')}
    ev_desc1_uid = mds.insert_descriptor(run_start=run_start,
                                         data_keys=data_keys1,
                                         time=common.get_time(),
                                         uid=str(uuid.uuid4()))
    ev_desc2_uid = mds.insert_descriptor(run_start=run_start,
                                         data_keys=data_keys2,
                                         time=common.get_time(),
                                         uid=str(uuid.uuid4()))
    # Create Events.
    events = []

    # Point Detector Events
    base_time = common.get_time()
    for i in range(num_exposures):
        time = float(i + 0.5 * rs.randn()) + base_time
        data = {'point_det': point_det_data[i]}
        timestamps = {'point_det': time}
        event_uid = mds.insert_event(descriptor=ev_desc1_uid,
                                     seq_num=i, time=time, data=data,
                                     uid=str(uuid.uuid4()),
                                     timestamps=timestamps)
        event, = mds.find_events(uid=event_uid)
        events.append(event)

    # Temperature Events
    for i, (time, temp) in enumerate(zip(*deadbanded_ramp)):
        time = float(time) + base_time
        data = {'Tsam': temp,
                'Troom': temp + 10}
        timestamps = {'Tsam': time,
                      'Troom': time}
        event_uid = mds.insert_event(descriptor=ev_desc2_uid,
                                     time=time, data=data, seq_num=i,
                                     uid=str(uuid.uuid4()),
                                     timestamps=timestamps)
        event, = mds.find_events(uid=event_uid)
        events.append(event)
    return events


if __name__ == '__main__':
    import metadatastore.api as mdsc
    run_start_uid = mdsc.insert_run_start(scan_id=2032013,
                                          beamline_id='testbed',
                                          owner='tester',
                                          group='awesome-devs',
                                          project='Nikea',
                                          time=0.,
                                          uid=str(uuid.uuid4()),)

    run(run_start_uid)
