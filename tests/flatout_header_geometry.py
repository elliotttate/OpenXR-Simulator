"""Measure the yellow header accent's angle in composed stereo pixels."""
import math
import numpy as np
from PIL import Image
from flatout_frontend_e2e import quad_pixel

def measure_header_accent(capture,path,rect):
 pixels=np.asarray(Image.open(path).convert('RGB'));l,t,r,b=rect;result={}
 for eye in ('left','right'):
  ys=[];xs=[]
  for cy in np.linspace(t+8,b-8,43):
   a=quad_pixel(capture,(l,cy,l,cy),eye);z=quad_pixel(capture,(l+31,cy,l+31,cy),eye)
   y=round((a[1]+z[1])*.5);lo=max(0,math.ceil(a[0]));hi=min(pixels.shape[1],math.floor(z[0])+1)
   if not (0<=y<pixels.shape[0]) or hi<=lo:continue
   line=pixels[y,lo:hi].astype(float);mask=(line[:,0]>180)&(line[:,1]>85)&(line[:,2]<80)&(line[:,0]>line[:,1]*1.12)
   pos=np.flatnonzero(mask)
   if len(pos)<2:continue
   x=float(np.mean(pos+lo));xs.append((x-a[0])/(z[0]-a[0])*31);ys.append(cy-t)
  assert len(xs)>=24,('Too few visible accent samples',eye,len(xs))
  slope,offset=np.polyfit(ys,xs,1);residual=float(np.sqrt(np.mean((np.array(xs)-(slope*np.array(ys)+offset))**2)))
  result[eye]={'degrees':math.degrees(math.atan(-slope)),'samples':len(xs),'rms_canvas_pixels':residual}
 return result
