import unittest,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from flatout_button_audit import ink_bounds,inspect
import flatout_ui_atlas as atlas
from types import SimpleNamespace

GLYPHS={ord('A'):{'xoffset':2.,'yoffset':20.,'width':40.,'height':70.,'xadvance':44.},ord(' '):{'xoffset':-2.,'yoffset':120.,'width':5.,'height':5.,'xadvance':20.}}

class ButtonAuditTests(unittest.IsolatedAsyncioTestCase):
    def test_transparent_space_does_not_pull_ink_down(self):
        self.assertEqual(ink_bounds(' A ',GLYPHS,.5,42.),[11.,10.,31.,45.])
    def test_tracking_contributes_to_actual_glyph_spacing(self):
        self.assertEqual(ink_bounds('AA',GLYPHS,.5,48.),[1.,10.,45.,45.])
    async def audit(self,label_y=0.,icon_x=0.):
        nodes=[{'path':'ROOT','visible':True},
            {'path':'ROOT/Button','rect':[0,0,200,80],'visible':True},
            {'path':'ROOT/Button/WelcomeActionLabel','rect':[113,12.5+label_y,135,75.5+label_y],'visible':True,'text':'A'},
            {'path':'ROOT/Button/ActionIcon','rect':[66+icon_x,24,98+icon_x,56],'visible':True}]
        async def query(method,args):
            return {'size':{'px':22.,'py':63.},'presentation':{'font_resource':'DATA:/FONTS/FO_ALDO_THE_APACHE.FNT','font_scale':.5}}
        with patch('flatout_button_audit.font_glyphs',return_value=GLYPHS):
            return await inspect({'nodes':nodes,'message':{'open':True}},query,'.')
    async def test_balanced_group(self):
        result=await self.audit();self.assertTrue(result['passed']);self.assertEqual(len(result['checks']),1)
    async def test_rejects_top_biased_caption(self):
        result=await self.audit(label_y=-20);self.assertFalse(result['passed'])
    async def test_rejects_icon_outside_safe_inset(self):
        result=await self.audit(icon_x=-51);self.assertFalse(result['passed'])

    async def test_exact_bindings_disambiguate_repeated_paths_without_extra_queries(self):
        nodes=[{'path':'ROOT','node_index':0,'parent_index':-1}]
        binding={'local_size':[22.,63.],'font_resource':'DATA:/FONTS/FO_ALDO_THE_APACHE.FNT','font_scale':.5}
        for x in (0,300):
            parent=len(nodes)
            nodes.extend([{'path':'ROOT/Button','node_index':parent,'parent_index':0,'rect':[x,0,x+200,80],'visible':True},
                          {'path':'ROOT/Button/ActionIcon','node_index':parent+1,'parent_index':parent,'rect':[x+66,24,x+98,56],'visible':True},
                          {'path':'ROOT/Button/WelcomeActionLabel','node_index':parent+2,'parent_index':parent,'rect':[x+113,12.5,x+135,75.5],'visible':True,'text':'A','presentation':binding}])
        async def query(*args):self.fail('Exact-node metadata must not fall back to an ambiguous path query')
        with patch('flatout_button_audit.font_glyphs',return_value=GLYPHS):
            result=await inspect({'nodes':nodes,'message':{'open':True}},query,'.')
        self.assertTrue(result['passed']);self.assertEqual(len(result['checks']),2);self.assertEqual(result['unverified'],[])


class LeaderboardSamplingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sample_stops_before_an_unneeded_async_page_request(self):
        test=atlas.Atlas(SimpleNamespace(output=Path('unused'),pid=1,native_timeout=1),None,{})
        test.active_case={'expand':'leaderboard'};captures=[];rays=[]
        async def query(method,params=None):return {'busy':False} if method=='leaderboard.get_state' else {'open':False}
        async def capture(*args):captures.append(args);return {'id':len(captures),'targets':{'targets':[{'name':'page.down'}]}}
        async def variants(*args):pass
        async def ray(name):rays.append(name)
        test.query=query;test.capture=capture;test.variants=variants;test.ray=ray
        with patch.object(atlas,'state_signature',lambda s:str(s['id'])):
            await test.pages('Leaderboard','page.down',3,sample_only=True)
        self.assertEqual(len(captures),3);self.assertEqual(rays,['page.down','page.down'])
        self.assertEqual(test.active_case['fixture_checks'],[{'sampled_pages':3,'more_pages_available':True}])

    async def test_service_modal_is_not_automatically_confirmed(self):
        test=atlas.Atlas(SimpleNamespace(output=Path('unused'),pid=1,native_timeout=1),None,{})
        test.active_case={'expand':'leaderboard'}
        async def query(method,params=None):return {'busy':False} if method=='leaderboard.get_state' else {'open':True}
        test.query=query
        with self.assertRaises(atlas.UnknownModal):await test.pages('Leaderboard','page.down',3,sample_only=True)

if __name__=='__main__':unittest.main()
