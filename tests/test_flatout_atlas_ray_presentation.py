"""Compositor survey preparation must use the menu, never a hover tile."""
import copy,sys,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from flatout_ui_atlas import Atlas,FixtureUnavailable

class RayPresentation(unittest.IsolatedAsyncioTestCase):
    def runner(self,spatial=False,full=True):
        a=object.__new__(Atlas);a.args=SimpleNamespace(hand='right',settle=0);a.owned_pointers={};a.calls=[]
        plane={'id':1,'visibility':0,'size':[4.5,2.53125],'pose':{'position':[0,1.7,-3],'orientation':[0,0,0,1]}}
        tile={'id':2,'visibility':0,'size':[.6,.25],'pose':{'position':[0,1.2,-2.98],'orientation':[0,0,0,1]}}
        cap={'capture_id':'owned','crop':[0,0,1920,1080],'image_size':[1280,720],
             'geometry':{'surfaces':([plane] if full else [])+[tile],
                         'eyes':[{'eye':'left','viewport':[0,0,1920,1080],'fov':[-.7,.7,.7,-.7],
                                  'pose':{'position':[0,1.7,0],'orientation':[0,0,0,1]}}]}}
        async def query(method,params=None):
            if method=='vr.targets':return {'clicks':9}
            if method=='menu.get_state':return {'is_3d':spatial}
            if method=='vr.get_state':return {}
            raise AssertionError(method)
        async def tool(name,args):
            a.calls.append((name,copy.deepcopy(args)))
            if name=='capture_screenshot':return cap,[]
            if name=='hover_ui_at':return {'status':'input_delivered'},[]
            raise AssertionError(name)
        a.query=query;a.tool=tool;return a

    async def test_compositor_menu_receives_two_distinct_blank_hovers_on_full_plane(self):
        a=self.runner();await a.prepare_ray_presentation()
        h=[v for name,v in a.calls if name=='hover_ui_at']
        self.assertEqual(len(h),2);self.assertTrue(all(v['surface']['id']==1 for v in h))
        self.assertNotEqual(h[0]['x'],h[1]['x']);self.assertEqual(a.owned_pointers,{'right':'owned'})
        self.assertTrue(all(v['hand']=='right' for v in h))

    async def test_only_hover_tiles_is_not_accepted_as_a_menu_carrier(self):
        a=self.runner(full=False)
        with self.assertRaises(FixtureUnavailable):await a.prepare_ray_presentation()
        self.assertFalse(any(name=='hover_ui_at' for name,_ in a.calls));self.assertFalse(a.owned_pointers)

    async def test_spatial_menu_is_not_reinterpreted_as_a_flat_canvas(self):
        a=self.runner(spatial=True);await a.prepare_ray_presentation()
        self.assertEqual(a.calls,[])

if __name__=='__main__':unittest.main()
