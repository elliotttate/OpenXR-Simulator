"""Atlas integration keeps comfort evidence distinct from native source images."""
import base64
import copy
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('atlas_comfort_test',ROOT/'tools/flatout_ui_atlas.py')
atlas=importlib.util.module_from_spec(spec);spec.loader.exec_module(atlas)


def frame():
    return {'camera':{'origin':[0,0,0],'right':[1,0,0],'up':[0,1,0],'back':[0,0,1]},
            'panel':{'origin':[0,1.7,-3],'right':[1,0,0],'up':[0,1,0],'back':[0,0,-1]},
            'panel_width':4.6,'panel_height':2.5875}


def snapshot():
    return {'menu':{'current_menu':'TEST','is_3d':False,'items':[]},'message':{'open':False},'nodes':[],
            'targets':{'vr_frame':frame(),'targets':[{'name':'back','rect':[100,900,400,1000]}]}}


class TargetMapping(unittest.TestCase):
    def test_world_controls_use_explicit_full_canvas_plane(self):
        source=snapshot();original=copy.deepcopy(source)
        targets,missing=atlas.comfort_review_targets({},source)
        self.assertFalse(missing);self.assertEqual(source,original)
        self.assertEqual(targets[0]['canvas_size'],[1920,1080])
        self.assertEqual(targets[0]['surface']['size'],[4.6,2.5875])
        self.assertNotIn('surface_id',targets[0])

    def test_split_controls_keep_their_own_plane_and_reject_a_crossing_rectangle(self):
        source=snapshot();f=source['targets']['vr_frame'];f['panels']=[]
        for i,x in enumerate((-2,2)):
            plane=copy.deepcopy(f['panel']);plane['origin']=[x,1.7,-3]
            f['panels'].append({'id':str(i),'canvas_rect':[i*960,0,(i+1)*960,1080],
                                'panel':plane,'panel_width':4.6,'panel_height':2.5875})
        source['targets']['targets']=[{'name':'left','rect':[100,200,400,300]},
            {'name':'right','rect':[1500,200,1800,300]},{'name':'crossing','rect':[950,200,970,300]}]
        targets,missing=atlas.comfort_review_targets({},source)
        self.assertEqual([t['surface']['pose']['position'][0] for t in targets],[-2,2])
        self.assertTrue(all(t['surface']['size'][0]==4.6 for t in targets))
        self.assertEqual(missing[0]['name'],'crossing')

    def test_composition_carrier_excludes_single_eye_controller_overlays(self):
        source=snapshot();source['targets'].pop('vr_frame')
        capture={'geometry':{'surfaces':[{'id':1,'visibility':1,'size':[100,100]},
            {'id':2,'visibility':0,'size':[3.3,1.85625]}]}}
        targets,missing=atlas.comfort_review_targets(capture,source)
        self.assertEqual(targets[0]['surface_id'],2);self.assertFalse(missing)

    def test_unknown_spatial_and_outside_canvas_regions_remain_unmeasured(self):
        source=snapshot();source['menu'].update(is_3d=True,items=[{'index':0,'title':'Career','rendered_position':[1,2,3]}])
        source['targets']['targets']=[{'name':'bad','rect':[-1,0,30,40]}]
        measured,missing=atlas.comfort_review_targets({},source)
        self.assertFalse(measured);self.assertEqual({t['name'] for t in missing},{'bad','spatial.0'})


class ComfortCapture(unittest.IsolatedAsyncioTestCase):
    def runner(self,path,enabled=True):
        output=Path(path);(output/'images').mkdir();(output/'states').mkdir()
        args=SimpleNamespace(output=output,pid=1,current=True,native_timeout=1,owner_queue=None,
                             eye='both',width=1280,comfort_guides=enabled)
        case={'id':'test','title':'Test','group':'Test','status':'captured','states':[]}
        report={'states':[],'cases':[case],'catalog':{'discovered_count':0,'menus':[],'dialogs':[]}}
        runner=atlas.Atlas(args,None,report);runner.active_case=case;calls=[]
        raw=BytesIO();Image.new('RGB',(3,1),'black').save(raw,'JPEG');original=raw.getvalue()
        overlay=BytesIO();Image.new('RGB',(3,1),'green').save(overlay,'PNG')
        async def observe():return copy.deepcopy(snapshot())
        async def tool(name,request):
            calls.append((name,copy.deepcopy(request)))
            if name=='capture_screenshot':
                return {'capture_id':str(len(calls)),'image_size':[3,1]},[SimpleNamespace(data=base64.b64encode(original).decode())]
            self.assertEqual(name,'analyze_ui_visibility')
            return {'status':'analyzed','summary':{'target_count':1}},[
                SimpleNamespace(mimeType='image/png',data=base64.b64encode(overlay.getvalue()).decode())]
        runner.observe=observe;runner.tool=tool;runner.project_targets=lambda *args:[]
        return runner,calls,original

    async def test_ray_presentation_capture_preserves_the_actual_hover(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,calls,_=self.runner(tmp);runner.args.current=False;runner.args.ray_presentation=True
            async def prepare():raise AssertionError('Parking the ray would erase the requested hover')
            async def observe():
                s=snapshot();s['targets']['hovered']='back';return s
            runner.prepare_ray_presentation=prepare;runner.observe=observe
            await runner.capture('Back hover','hover','back')
            saved=json.loads((Path(tmp)/runner.report['states'][0]['metadata']).read_text())
            self.assertEqual(saved['targets']['hovered'],'back')
            self.assertEqual(sum(name=='capture_screenshot' for name,_ in calls),1)

    async def test_lost_hover_is_not_saved_as_a_successful_hover_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,calls,_=self.runner(tmp)
            with self.assertRaises(atlas.CaptureMismatch):await runner.capture('Lost hover','hover','back')
            self.assertEqual(calls,[]);self.assertFalse(runner.report['states'])

    async def test_optional_analysis_saves_separate_guide_without_input_or_source_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,calls,original=self.runner(tmp)
            await runner.capture('One')
            state=runner.report['states'][0]
            self.assertEqual([c[0] for c in calls],['capture_screenshot','analyze_ui_visibility'])
            self.assertEqual((Path(tmp)/state['image']).read_bytes(),original)
            self.assertEqual(state['comfort']['status'],'analyzed')
            self.assertTrue((Path(tmp)/state['comfort']['image']).is_file())
            self.assertIn('Diagnostic guide',(Path(tmp)/'index.html').read_text(encoding='utf-8'))
            derived=Path(tmp)/'derived';atlas.reprocess_report(tmp,derived)
            self.assertEqual((derived/state['comfort']['image']).read_bytes(),(Path(tmp)/state['comfort']['image']).read_bytes())

    async def test_comfort_mode_takes_fresh_frames_while_default_layout_dedup_stays_fast(self):
        for enabled,expected in ((True,2),(False,1)):
            with self.subTest(enabled=enabled),tempfile.TemporaryDirectory() as tmp:
                runner,calls,_=self.runner(tmp,enabled)
                await runner.capture('One');await runner.capture('Two')
                self.assertEqual(sum(name=='capture_screenshot' for name,_ in calls),expected)
                self.assertEqual(sum(name=='analyze_ui_visibility' for name,_ in calls),2 if enabled else 0)

    async def test_analysis_errors_are_visible_and_leave_the_native_capture_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,calls,original=self.runner(tmp);tool=runner.tool
            async def fail(name,request):
                if name=='analyze_ui_visibility':return {'status':'error','error':'Unavailable'},[]
                return await tool(name,request)
            runner.tool=fail;await runner.capture('One')
            state=runner.report['states'][0]
            self.assertEqual(state['comfort']['status'],'error')
            self.assertEqual((Path(tmp)/state['image']).read_bytes(),original)
            self.assertNotIn('image',state['comfort'])

    async def test_saved_opening_reference_is_used_without_moving_the_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner,calls,_=self.runner(tmp)
            runner.neutral_head_pose={'x':.1,'y':1.7,'z':.2,'yaw_deg':0,'pitch_deg':0,'roll_deg':0}
            await runner.capture('Glance')
            request=next(args for name,args in calls if name=='analyze_ui_visibility')
            self.assertEqual(request['reference_pose'],{'position':[.1,1.7,.2],'orientation':[0,0,0,1]})
            self.assertFalse(any(name=='set_head_pose' for name,_ in calls))


if __name__=='__main__':unittest.main()
