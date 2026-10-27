"""Offline controls for native material timeline qualification; no game access."""
import asyncio
import time
import unittest

from flatout_challenge_material_timeline_e2e import (MaterialSampler,material_vector,
    qualify_photo,qualify_footer)
from flatout_challenge_timeline_e2e import cubic


def curve(x,authored=True):
    if x<=0:return 0.
    if x>=1:return 1.
    low,high=0.,1.
    for _ in range(60):
        mid=(low+high)/2
        if cubic(mid,.2 if authored else .25,.2 if authored else .25)<x:low=mid
        else:high=mid
    return cubic((low+high)/2,.75 if authored else .1,1.)


def photo_trace(speed=1):
    result=[]
    for i in range(161):
        t=i/100;zoom=curve(t*speed);grade=curve(t*speed/.7,False);sheen=curve(t*speed/1.05)
        result.append({'time':100+t,'active':True,'query_seconds':.001,
                       'vector':[1.005+.05*zoom,.91+.17*grade,.86+.24*grade,sheen]})
    return {'samples':result}


def footer_trace():
    return {'samples':[{'time':200+i/100,'active':True,'query_seconds':.001,
            'vector':[curve(i/100/.35),curve(i/100/.7),529.,163.]} for i in range(121)]}


class MaterialTimelines(unittest.TestCase):
    def test_authored_photo_timings_are_independently_observed(self):
        tracks=qualify_photo(photo_trace())
        self.assertEqual(len(tracks),4)
        for track in tracks:self.assertAlmostEqual(track['estimated_seconds'],track['expected_seconds'],places=5)

    def test_double_ticked_photo_does_not_pass(self):
        with self.assertRaises(AssertionError):qualify_photo(photo_trace(2))

    def test_static_photo_is_not_a_timeline(self):
        trace=photo_trace()
        for sample in trace['samples']:sample['vector']=[1.055,1.08,1.1,1]
        with self.assertRaises(AssertionError):qualify_photo(trace)

    def test_missing_native_material_is_not_a_skip(self):
        for node in ({},{'material':{'available':False,'vectors':{},'missing':['g_PhotoDynamics']}}):
            with self.assertRaises(AssertionError):material_vector(node,'g_PhotoDynamics')

    def test_missing_vector_never_falls_back_to_a_default(self):
        with self.assertRaises(AssertionError):material_vector({'material':{
            'available':True,'vectors':{},'missing':['g_StyleState']}},'g_StyleState')

    def test_nonfinite_uniform_is_rejected(self):
        with self.assertRaises(AssertionError):material_vector({'material':{
            'available':True,'vectors':{'g_StyleState':[0,float('nan'),529,163]},'missing':[]}},'g_StyleState')

    def test_footer_mix_and_sheen_have_separate_durations(self):
        tracks=qualify_footer(footer_trace())
        self.assertAlmostEqual(tracks[0]['estimated_seconds'],.35,places=5)
        self.assertAlmostEqual(tracks[1]['estimated_seconds'],.7,places=5)

    def test_footer_paint_size_must_not_drift(self):
        trace=footer_trace();trace['samples'][30]['vector'][2]+=1
        with self.assertRaises(AssertionError):qualify_footer(trace)

    def test_brightness_and_saturation_cannot_use_different_tracks(self):
        trace=photo_trace();trace['samples'][20]['vector'][2]+=.01
        with self.assertRaises(AssertionError):qualify_photo(trace)

    def test_sampler_runs_while_event_loop_is_blocked(self):
        class Fixture:
            def call(self,method,params=None):
                if method!='hud.get_node':raise AssertionError('Unexpected native operation '+method)
                time.sleep(.004)
                return {'name':'ChallengeTrackArt','material':{'available':True,
                    'vectors':{'g_PhotoDynamics':[1.005,.91,.86,0]},'missing':[]}}
        async def verify():
            sampler=MaterialSampler(0,.4,'Menu/ChallengeTrackArt','g_PhotoDynamics',Fixture())
            sampler.thread.start();self.assertTrue(await asyncio.to_thread(sampler.ready.wait,1))
            before=len(sampler.evidence()['samples']);time.sleep(.16)
            after=len(sampler.evidence()['samples']);await sampler.finish()
            self.assertGreaterEqual(after-before,6);self.assertFalse(sampler.thread.is_alive())
        asyncio.run(verify())


if __name__=='__main__':unittest.main()
