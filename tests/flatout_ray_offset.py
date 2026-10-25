"""Read-only calibration for FlatOut's controller-shell ray emitters."""
import numpy as np


def native_pointer_offset(state, hand):
    controllers=state.get('controllers',[])
    if len(controllers)!=2:
        return None
    controller=controllers[int(hand=='right')]
    if not controller.get('valid') or not controller.get('pointer_pose_valid'):
        return None
    aim,pointer=controller['pose'],controller['pointer_pose']
    basis=np.column_stack([aim[k] for k in ('x','y','z')])
    local=basis.T @ (np.asarray(pointer['position'])-aim['position'])
    assert np.all(np.isfinite(local)) and np.linalg.norm(local)<1, 'Invalid native controller emitter offset'
    return {'position':local.tolist(),'orientation':[0,0,0,1],'position_space':'aim'}
