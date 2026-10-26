"""Centered livery tasks expose their own controls, preserving raw evidence."""
import copy
import importlib.util
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('atlas_inventory', ROOT/'tools/flatout_ui_atlas.py')
atlas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(atlas)


def snapshot(task='Livery keyboard'):
    surface = {'id':1, 'visibility':0, 'size':[4.6,2.5875]}
    source = {'menu':{'current_menu':'SKINS','items':[]}, 'message':{'open':False}, 'nodes':[],
        'targets':{'targets':[
            {'name':'livery.1','label':'Rear','enabled':True,'rect':[344,360,488,404]},
            {'name':'livery.2','label':'Q','enabled':True,'rect':[330,370,440,420]},
            {'name':'livery.3','label':'Cancel','enabled':True,'rect':[1090,682,1240,745]}]},
        'livery':{'vr':{'active':True,'frame':{},'controls':[
            {'id':1,'label':'Rear','window':'Livery camera rail','enabled':True,'x':344,'y':360,'width':144,'height':44},
            {'id':2,'label':'Q','window':task,'enabled':True,'x':330,'y':370,'width':110,'height':50},
            {'id':3,'label':'Cancel','window':task,'enabled':True,'x':1090,'y':682,'width':150,'height':63}]},
            'desktop_input':{'popup_windows':[{'name':task,'captured':True}]}}}
    return source, {'geometry':{'surfaces':[surface]}}


class CenteredTaskInventory(unittest.TestCase):
    def test_keyboard_and_workshop_hide_tool_columns_from_exposed_controls(self):
        for task in ('Livery keyboard','Browse Workshop liveries','Share livery to Workshop'):
            with self.subTest(task=task):
                source,_ = snapshot(task); original=copy.deepcopy(source)
                inventory=atlas.control_inventory(source)
                self.assertEqual([c['name'] for c in inventory['controls']],['livery.2','livery.3'])
                self.assertEqual([c['name'] for c in inventory['background_controls']],['livery.1'])
                self.assertFalse(inventory['background_controls'][0]['enabled'])
                self.assertEqual(inventory['background_controls'][0]['hidden_by'],'centered_livery_task')
                self.assertEqual(source,original)

    def test_comfort_review_uses_only_exposed_rectangles(self):
        source,capture=snapshot()
        targets,missing=atlas.comfort_review_targets(capture,source)
        self.assertEqual([t['name'] for t in targets],['livery.2','livery.3'])
        self.assertEqual(missing,[])

    def test_hidden_popup_does_not_remove_the_normal_tool_controls(self):
        source,_=snapshot();source['livery']['desktop_input']['popup_windows'][0]['captured']=False
        self.assertEqual(len(atlas.control_inventory(source)['controls']),3)
        self.assertEqual(atlas.control_inventory(source)['background_controls'],[])

    def test_unknown_or_absent_popup_metadata_does_not_guess_visibility(self):
        for windows in ([],[{'name':'Unknown popup','captured':True}]):
            source,_=snapshot();source['livery']['desktop_input']['popup_windows']=windows
            self.assertEqual(len(atlas.control_inventory(source)['controls']),3)

    def test_captured_child_popup_controls_are_retained(self):
        source,_=snapshot();source['livery']['vr']['controls'][1]['window']='Keyboard choices'
        source['livery']['desktop_input']['popup_windows'].append({'name':'Keyboard choices','captured':True})
        self.assertEqual([c['name'] for c in atlas.control_inventory(source)['controls']],['livery.2','livery.3'])

    def test_old_modal_capture_never_calls_livery_controls_interactive(self):
        source,_=snapshot();source['message']['open']=True
        self.assertEqual(atlas.control_inventory(source)['controls'],[])
        self.assertEqual(len(atlas.control_inventory(source)['background_controls']),3)

    def test_summary_counts_only_the_current_task_controls(self):
        source,_=snapshot()
        state=atlas.control_inventory(source)
        summary=atlas.coverage_summary({'states':[state]})
        self.assertEqual(summary['unique_controls'],2)
        self.assertEqual(summary['control_observations'],2)

    def test_projected_overlays_exclude_hidden_column_controls_in_both_eyes(self):
        source,capture=snapshot()
        capture.update(crop=[0,0,1280,640],image_size=[1280,640])
        capture['geometry']['surfaces'][0]['pose']={'position':[0,1.7,-3],'orientation':[0,0,0,1]}
        capture['geometry']['eyes']=[{'eye':name,'viewport':[x,0,640,640],
            'pose':{'position':[0,1.7,0],'orientation':[0,0,0,1]},
            'fov':[-math.pi/4,math.pi/4,math.pi/4,-math.pi/4]}
            for name,x in (('left',0),('right',640))]
        runner=atlas.Atlas(SimpleNamespace(pid=1,output=Path('unused'),native_timeout=1,eye='both'),None,{})
        overlays=runner.project_targets(capture,source)
        self.assertEqual({(o['name'],o['eye']) for o in overlays},
                         {(name,eye) for name in ('livery.2','livery.3') for eye in ('left','right')})

    def test_reprocessing_corrects_overlays_and_counts_without_altering_raw_evidence(self):
        source,_=snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source';path.mkdir()
            raw=json.dumps(source).encode();pixels=b'original native screenshot bytes'
            (path/'state.json').write_bytes(raw);(path/'image.jpg').write_bytes(pixels)
            state={**atlas.control_inventory(source),'metadata':'state.json','image':'image.jpg',
                   'overlays':[{'name':'livery.1','eye':'left'},{'name':'livery.2','eye':'left'}]}
            (path/'report.json').write_text(json.dumps({'states':[state],'cases':[]}),encoding='utf-8')
            derived=Path(tmp)/'derived';result=atlas.reprocess_report(path,derived)
            self.assertEqual([o['name'] for o in result['states'][0]['overlays']],['livery.2'])
            self.assertEqual(result['states'][0]['visible_control_names'],['livery.2'])
            self.assertEqual(result['states'][0]['offscreen_control_names'],['livery.3'])
            self.assertEqual((derived/'state.json').read_bytes(),raw)
            self.assertEqual((derived/'image.jpg').read_bytes(),pixels)
            self.assertEqual((path/'state.json').read_bytes(),raw)

    def test_older_comfort_assets_are_labelled_when_inventory_is_corrected(self):
        source,_=snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source';path.mkdir()
            (path/'state.json').write_text(json.dumps(source),encoding='utf-8')
            (path/'old-comfort.json').write_text('{"original":true}',encoding='utf-8')
            state={'metadata':'state.json','controls':[{'name':'livery.1'},{'name':'livery.2'},{'name':'livery.3'}],
                   'comfort':{'status':'analyzed','report':'old-comfort.json','summary':{'target_count':3}}}
            (path/'report.json').write_text(json.dumps({'states':[state],'cases':[]}),encoding='utf-8')
            result=atlas.reprocess_report(path,Path(tmp)/'derived')
            review=result['states'][0]['comfort']
            self.assertEqual(review['status'],'source_inventory_changed')
            self.assertNotIn('summary',review)
            self.assertEqual(review['source_summary'],{'target_count':3})
            self.assertEqual((Path(tmp)/'derived/old-comfort.json').read_bytes(),(path/'old-comfort.json').read_bytes())


class NativeModalObservation(unittest.IsolatedAsyncioTestCase):
    async def test_game_message_box_keeps_native_modal_targets_in_livery(self):
        source,_=snapshot();source['message']['open']=True
        source['targets']['targets']=[{'name':'msgbox.validate','rect':[800,700,1100,780]}]
        args=SimpleNamespace(pid=1,output=Path('unused'),current=True,native_timeout=1,owner_queue=None)
        runner=atlas.Atlas(args,None,{'cases':[],'states':[]});runner.active_case={'id':'livery','expand':'livery'}
        calls=[]
        async def query(method,params=None):
            calls.append(method)
            return copy.deepcopy({'menu.get_state':source['menu'],'vr.targets':source['targets'],
                'ui.get_message_box':source['message'],'hud.rects':{'nodes':[]},'livery.command':source['livery']}[method])
        runner.query=query
        observed=await runner.observe()
        self.assertEqual(observed['targets']['targets'],source['targets']['targets'])
        self.assertEqual([c['name'] for c in atlas.control_inventory(observed)['controls']],['msgbox.validate'])
        self.assertTrue(all(name in atlas.READ_METHODS or name=='livery.command' for name in calls))


if __name__=='__main__':unittest.main()
