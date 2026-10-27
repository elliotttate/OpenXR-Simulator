"""Keep material/animation oracles honest without launching the native game."""
import unittest
import numpy as np
from flatout_challenge_style_e2e import (directional_metric, normalized_error,
    shine_metrics, outside, grid, SOURCE_ZOOM, saturation, LUMA,
    source_effect_prediction,source_effect_metrics,reference_chroma_signal,ChallengeStyle,
    register_footer_y,bilinear)


class StyleMetrics(unittest.TestCase):
    def test_identical_photo_cannot_pass_brightness_or_saturation_direction(self):
        rest=np.full(1024,.3)
        self.assertFalse(directional_metric(rest,rest,rest,1)['passed'])
        self.assertFalse(directional_metric(rest,rest,rest,-1)['passed'])

    def test_visible_grade_exceeds_independently_observed_noise(self):
        rng=np.random.default_rng(9);rest=rng.uniform(.1,.6,1024)
        repeat=rest+rng.uniform(-.001,.001,1024)
        self.assertTrue(directional_metric(rest,repeat,repeat+.04,1)['passed'])
        self.assertTrue(directional_metric(rest,repeat,repeat-.04,-1)['passed'])

    def test_repeat_noise_is_not_a_material_response(self):
        rest=np.full(1024,.3);repeat=rest+.025
        result=directional_metric(rest,repeat,repeat+.01,1)
        self.assertFalse(result['passed'])

    def test_brightness_only_is_removed_by_shape_metric(self):
        x=np.linspace(0,1,128)
        a=np.column_stack([x*x,x,np.sqrt(x)])*.5+.05
        self.assertLess(normalized_error(a,a*1.18+.01),1e-20)

    def test_brightness_alone_cannot_be_reported_as_saturation_gain(self):
        rng=np.random.default_rng(12);rgb=rng.uniform(.04,.6,(1024,3))
        weight=np.sqrt(rgb@LUMA)
        result=directional_metric(saturation(rgb)*weight,saturation(rgb)*weight,
                                  saturation(rgb*1.18)*weight,1)
        self.assertFalse(result['passed'])

    def test_static_highlight_cannot_pass_as_a_moving_shine(self):
        x=np.linspace(0,1,96)
        fixed=np.exp(-((x-.4)/.12)**2)*.08
        result=shine_metrics([(0,fixed),(.2,fixed),(.5,fixed),(1,np.zeros(96))],np.zeros(96))
        self.assertFalse(result['passed'])
        self.assertFalse(result['resolved_forward_motion'])

    def test_unsampled_or_absent_shine_is_an_explicit_failure(self):
        result=shine_metrics([(0,np.zeros(96)),(.9,np.zeros(96))],np.zeros(96))
        self.assertFalse(result['passed'])

    def test_travelling_pulse_that_fades_is_observable(self):
        x=np.linspace(0,1,96)
        profiles=[(t,.08*np.exp(-((x-c)/.12)**2)) for t,c in ((.1,.2),(.3,.5),(.5,.8))]
        profiles.append((1,np.zeros(96)))
        self.assertTrue(shine_metrics(profiles,np.zeros(96))['passed'])

    def test_late_moving_noise_cannot_qualify_a_missed700ms_window(self):
        x=np.linspace(0,1,96)
        profiles=[(t,.08*np.exp(-((x-c)/.12)**2)) for t,c in ((.9,.2),(1.2,.5),(1.5,.8))]
        profiles.append((2,np.zeros(96)))
        result=shine_metrics(profiles,np.zeros(96))
        self.assertFalse(result['passed']);self.assertEqual(result['resolved_early_pulses'],0)

    def test_new_hover_fill_alone_is_not_a_shine(self):
        x=np.linspace(0,1,96);fill=.04+.02*x
        profiles=[(t,(1-np.exp(-t/.08))*fill-fill) for t in (.05,.15,.3,.5,1)]
        self.assertFalse(shine_metrics(profiles,np.zeros(96))['passed'])

    def test_single_column_noise_cannot_be_a_resolved_light_band(self):
        profiles=[]
        for t,column in ((.1,12),(.3,48),(.5,80)):
            p=np.zeros(96);p[column]=.03;profiles.append((t,p))
        profiles.append((1,np.zeros(96)))
        self.assertFalse(shine_metrics(profiles,np.zeros(96))['passed'])

    def test_small_centroid_jitter_is_not_a_forward_sweep(self):
        x=np.linspace(0,1,96)
        profiles=[(t,.08*np.exp(-((x-c)/.12)**2)) for t,c in ((.1,.45),(.3,.48),(.5,.51))]
        profiles.append((1,np.zeros(96)))
        self.assertFalse(shine_metrics(profiles,np.zeros(96))['passed'])

    def test_persistent_highlight_does_not_count_as_fading(self):
        x=np.linspace(0,1,96)
        profiles=[(t,.08*np.exp(-((x-c)/.12)**2)) for t,c in ((.1,.2),(.3,.5),(.5,.8),(1,.8))]
        self.assertFalse(shine_metrics(profiles,np.zeros(96))['passed'])

    def test_registered_footer_edges_remove_the_known_four_pixel_pose_change(self):
        action={'x':100,'y':200,'width':440,'height':73}
        class Fixture:
            def __init__(self,offset):self.offset=offset
            def lift_at(self,target):return .02
            def samples(self,points,eye,lift=0):
                top=action['y']+self.offset;bottom=top+action['height']
                alpha=np.clip(np.minimum(points[:,1]-top,bottom-points[:,1])+.5,0,1)
                rgb=np.array([.1,.1,.1])+alpha[:,None]*np.array([.7,.5,-.05])
                return rgb,np.ones(len(points),dtype=bool)
        pose,_=register_footer_y(Fixture(-1.75),Fixture(-4),action,'left',[100,200,540,280])
        self.assertAlmostEqual(pose['offset_y'],-1.75)
        self.assertLess(pose['edge_error'],1e-20)

    def test_uint8_storage_preserves_float_roi_interpolation(self):
        pixels=np.arange(12*12*3,dtype=np.uint16).reshape(12,12,3).astype(np.uint8)
        points=np.array([[2.25,3.5],[6.75,8.125]])
        np.testing.assert_allclose(bilinear(pixels,points)/255,bilinear(pixels.astype(float)/255,points),atol=1e-15)

    def test_source_shadow_plus_glow_is_not_required_to_brighten_orange_world(self):
        rest=np.tile([.72,.45,.29],(512,1))
        old=np.tile([.8,.6,.02,.01],(512,1))
        new=np.tile([.4,.3,.00,.06],(512,1))
        actual=source_effect_prediction(rest,old,new)
        self.assertLess(float(np.mean(actual-rest)),0)
        self.assertTrue(source_effect_metrics(rest,rest,actual,old,new)['passed'])

    def test_source_effect_oracle_strictly_rejects_unchanged_and_black_shadow_only(self):
        rest=np.tile([.72,.45,.29],(512,1))
        old=np.tile([.8,.6,.02,.01],(512,1));new=np.tile([.4,.3,.00,.06],(512,1))
        self.assertFalse(source_effect_metrics(rest,rest,rest,old,new)['passed'])
        shadow=source_effect_metrics(rest,rest,rest*.96,old,new)
        self.assertFalse(shadow['passed'])
        self.assertFalse(shadow['chromatic_response_passed'])

    def test_independent_analysis_failure_is_retained_without_aborting_next_phase(self):
        owner=object.__new__(ChallengeStyle);owner.cases=[]
        def failed():raise AssertionError('Missing visible glow')
        owner.visual_phase('glow',failed)
        owner.visual_phase('next',lambda:owner.cases.append({'case':'next','passed':True}))
        self.assertEqual([c['passed'] for c in owner.cases],[False,True])

    def test_reference_roi_selection_has_no_hover_image_input(self):
        rest=np.tile([.72,.45,.29],(512,1));old=np.tile([.8,.6,.02,.01],(512,1))
        strong=np.tile([.7,.65,.00,.08],(512,1));weak=np.tile([.4,.3,.00,.02],(512,1))
        self.assertGreater(reference_chroma_signal(rest,rest,old,strong)['selection_score'],
                           reference_chroma_signal(rest,rest,old,weak)['selection_score'])

    def test_badge_and_tag_are_removed_from_photo_grid(self):
        points=grid([0,0,100,60],10,6)
        keep=outside(points,[{'x':30,'y':30,'width':40,'height':30}],padding=0)
        self.assertEqual(int(np.count_nonzero(keep)),48)
        self.assertGreater(SOURCE_ZOOM,1)


if __name__=='__main__':unittest.main()
