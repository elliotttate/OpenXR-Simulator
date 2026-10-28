"""Safety of opening the real owning challenge callback without starting a race."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import flatout_ui_atlas as atlas


class SingleCarStagingTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self,items,ready=True,modal=False):
        a=object.__new__(atlas.Atlas);a.args=SimpleNamespace(current=False,fixture_timeout=0,fixture=[],ray_presentation=True)
        a.active_case={};a.calls=[]
        async def query(method,params=None):
            a.calls.append((method,params))
            if method=='menu.get_state':return {'items':items}
            if method=='ui.get_message_box':return {'open':modal}
            if method=='vr.targets':return {'targets':[{'name':'helper.Validate.1','enabled':ready}]}
            raise AssertionError(method)
        async def select(index):a.calls.append(('owning-select',index))
        async def fixture_menu(*args,**kwargs):a.calls.append(('fixture-menu',args));return {}
        async def ray_mode():a.calls.append(('hover-only-ray-mode',None))
        a.query=query;a.select=select;a.fixture_menu=fixture_menu
        a.prepare_ray_presentation=ray_mode
        original=atlas.Atlas.normal_flow
        async def normal_flow(alias):
            if alias=='challenge':a.calls.append(('normal-challenge',None));return {}
            return await original(a,alias)
        a.normal_flow=normal_flow
        return a

    async def test_only_unlocked_installed_event_is_opened_and_start_is_never_sent(self):
        a=self.fixture([{'index':0,'title':'Locked','locked':True},
            {'index':1,'title':'Missing','locked':False,'installed':False},
            {'index':2,'title':'Forest Arena','locked':False,'installed':True}])
        await a.normal_flow('single_car')
        self.assertEqual([c for c in a.calls if c[0]=='owning-select'],[('owning-select',2)])
        self.assertTrue(a.active_case['fixture_checks'][0]['vehicle_ready'])
        self.assertFalse(any(c[0] in ('menu.go_to','menu.validate','click_ui_at') for c in a.calls))

    async def test_no_unlock_or_fake_configuration_when_every_event_is_unavailable(self):
        a=self.fixture([{'index':0,'title':'Locked','locked':True}])
        with self.assertRaises(atlas.FixtureUnavailable):await a.normal_flow('single_car')
        self.assertFalse(any(c[0]=='owning-select' for c in a.calls))

    async def test_disabled_start_does_not_count_as_ready_or_get_activated(self):
        a=self.fixture([{'index':0,'title':'Total Destruction','locked':False}],ready=False)
        with self.assertRaises(atlas.FixtureUnavailable):await a.normal_flow('single_car')
        self.assertEqual(len([c for c in a.calls if c[0]=='owning-select']),1)
        self.assertNotIn('fixture_checks',a.active_case)

    async def test_unknown_real_modal_stops_in_place(self):
        a=self.fixture([{'index':0,'title':'Total Destruction','locked':False}],modal=True)
        with self.assertRaises(atlas.FixtureUnavailable):await a.normal_flow('single_car')
        self.assertFalse(any(c[0]=='ui.confirm_message_box' for c in a.calls))

    async def test_controller_survey_preserves_input_mode_and_does_not_claim_ray_readiness(self):
        a=self.fixture([{'index':0,'title':'Total Destruction','locked':False}]);a.args.ray_presentation=False
        await a.normal_flow('single_car')
        self.assertFalse(any(c[0] in ('hover-only-ray-mode','vr.targets') for c in a.calls))
        self.assertEqual(a.active_case['fixture_checks'][0]['vehicle_ready'],'unverified in controller presentation')

    def test_manifest_uses_the_owning_route_instead_of_uninitialized_go_to(self):
        manifest=atlas.load_manifest(atlas.DEFAULT_MANIFEST)
        case=next(c for c in manifest['cases'] if c['id']=='single-car')
        self.assertEqual(case['route'],[{'goto':'single_car'}]);self.assertIn('single_car',atlas.NORMAL_FLOWS)
        self.assertFalse(atlas.case_fixture_requirements(case))


if __name__=='__main__':unittest.main()
