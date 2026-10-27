"""Spatial-menu review uses native layout geometry, including visible Back."""
import copy
import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('spatial_atlas',ROOT/'tools/flatout_ui_atlas.py')
atlas=importlib.util.module_from_spec(spec);spec.loader.exec_module(atlas)


def snapshot():
    camera={'origin':[10,20,30],'right':[1,0,0],'up':[0,0,1],'back':[0,-1,0]}
    bounds={'space':'game_world','kind':'hud_layout_quad','visible':True,
            'corners':[[9.5,23,31.6],[10.5,23,31.6],[10.5,23,31.4],[9.5,23,31.4]]}
    return {'menu':{'current_menu':'MAIN MENU','is_3d':True,
        'spatial_layout':{'origin':[10,23,31.5],'right':[1,0,0],'up':[0,0,1],'back':[0,1,0]},
        'items':[{'index':0,'title':'Career','rendered_position':[10,23,31.5],'rendered_bounds':bounds}]},
        'targets':{'targets':[],'vr_frame':{'camera':camera}},'message':{'open':False},'nodes':[]}


class SpatialReview(unittest.TestCase):
    def test_native_label_bounds_map_to_xr_local_without_guessing_ray_hit_area(self):
        source=snapshot();original=copy.deepcopy(source)
        targets,missing=atlas.comfort_review_targets({},source)
        self.assertFalse(missing);self.assertEqual(len(targets),1)
        self.assertEqual(targets[0]['name'],'spatial.0')
        self.assertEqual(targets[0]['role'],'panel')
        self.assertEqual(targets[0]['surface']['pose']['position'],[0,1.5,-3])
        self.assertAlmostEqual(targets[0]['surface']['size'][0],1)
        self.assertAlmostEqual(targets[0]['surface']['size'][1],.2)
        self.assertEqual(source,original)

    def test_back_button_is_inventoried_and_retains_nonuniform_layout_scale(self):
        source=snapshot();layout=source['menu']['spatial_layout']
        layout.update(right=[2,0,0],up=[0,0,3])
        source['menu']['spatial_back']={'position':[10,23,30.5],'half_extents':[.4,.1]}
        controls=atlas.control_inventory(source)['controls']
        self.assertEqual([c['name'] for c in controls],['spatial.0','spatial.back'])
        targets,missing=atlas.comfort_review_targets({},source)
        self.assertFalse(missing)
        back=next(t for t in targets if t['name']=='spatial.back')
        self.assertEqual(back['role'],'control')
        self.assertAlmostEqual(back['surface']['size'][0],1.6)
        self.assertAlmostEqual(back['surface']['size'][1],.6)

    def test_modal_blocks_spatial_labels_and_back_from_comfort_and_exposed_inventory(self):
        source=snapshot();source['message']['open']=True
        source['menu']['spatial_back']={'position':[10,23,30.5],'half_extents':[.4,.1]}
        inventory=atlas.control_inventory(source)
        self.assertFalse(inventory['controls']);self.assertEqual(len(inventory['background_controls']),2)
        self.assertEqual(atlas.comfort_review_targets({},source),([],[]))

    def test_missing_camera_or_malformed_native_geometry_remains_unmeasured(self):
        for mutation in (
            lambda s:s['targets'].pop('vr_frame'),
            lambda s:s['menu']['items'][0]['rendered_bounds'].update(space='unknown'),
            lambda s:s['menu']['items'][0]['rendered_bounds']['corners'][2].__setitem__(0,99),
        ):
            source=snapshot();mutation(source)
            targets,missing=atlas.comfort_review_targets({},source)
            self.assertFalse(targets);self.assertEqual(missing[0]['name'],'spatial.0')

    def test_native_hidden_label_is_not_reported_as_exposed(self):
        source=snapshot();source['menu']['items'][0]['rendered_bounds']['visible']=False
        inventory=atlas.control_inventory(source)
        self.assertFalse(inventory['controls'])
        self.assertEqual(inventory['background_controls'][0]['hidden_by'],'native_visibility')

    def test_overlay_projects_native_spatial_corners_in_both_eyes(self):
        source=snapshot()
        capture={'image_size':[1280,640],'crop':[0,0,1280,640],
            'geometry':{'projection_eyes':[{'eye':name,'viewport':[x,0,640,640],
                'pose':{'position':[0,1.5,0],'orientation':[0,0,0,1]},
                'fov':[-math.pi/4,math.pi/4,math.pi/4,-math.pi/4]}
                for name,x in (('left',0),('right',640))]}}
        runner=atlas.Atlas(SimpleNamespace(pid=1,output=Path('unused'),native_timeout=1,eye='both'),None,{})
        overlays=runner.project_targets(capture,source)
        self.assertEqual({(o['name'],o['eye']) for o in overlays},{('spatial.0','left'),('spatial.0','right')})
        self.assertAlmostEqual(overlays[0]['rect'][2]-overlays[0]['rect'][0],640/6,places=1)


if __name__=='__main__':unittest.main()
