"""Owned LAN discovery fixture for the native FlatOut room browser.

Replies only to the specified local game's UDP source port and local addresses.
It implements offline discovery only: no connections, joins or gameplay. Callers
must verify that the port belongs to their isolated test PID before starting.
"""
import re,select,socket,threading,time
from pathlib import Path

MAGIC=bytes.fromhex('00ffff00fefefefefdfdfdfd12345678')

class LanDiscoveryFixture:
 def __init__(self,fox,game_port,count=9,published=None):
  assert 1<=count<=12 and 1024<game_port<65523
  source=Path(fox)/'PlayAll/External/Common/Current/PlayAll/External/RakNet-4.0802/Source/MessageIdentifiers.h'
  text=source.read_text().split('enum DefaultMessageIDTypes',1)[1].split('};',1)[0]
  ids=re.findall(r'^\s*(ID_[A-Z0-9_]+)\s*,',text,re.M)
  self.pong=ids.index('ID_UNCONNECTED_PONG');assert ids[1:3]==['ID_UNCONNECTED_PING','ID_UNCONNECTED_PING_OPEN_CONNECTIONS']
  self.game_port=game_port;self.count=count;self.published=published or {};self.rows=[];self.sockets=[];self.stop=threading.Event();self.lock=threading.Lock();self.replies=0;self.rejected=0
  self.addresses={'127.0.0.1',*socket.gethostbyname_ex(socket.gethostname())[2]}
 def __enter__(self):
  # Obtain the actual build's game/lobby identification, without entering it.
  with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as probe:
   probe.bind(('127.0.0.1',0));probe.settimeout(2)
   for width in (4,8):
    stamp=(int(time.monotonic()*1000)&((1<<(8*width))-1)).to_bytes(width,'big')
    probe.sendto(b'\x01'+stamp+MAGIC+(0xA91400000000FFF0).to_bytes(8,'big'),('127.0.0.1',self.game_port))
    try:data,_=probe.recvfrom(1024)
    except socket.timeout:continue
    at=data.find(MAGIC);assert data[0]==self.pong and at in (13,17),data.hex()
    response=data[at+16:];sep=response.find(b'\x1d');assert sep>0,'Owned game is not publishing native room status'
    self.prefix=response[:sep];break
   else:raise AssertionError('Owned game did not answer local offline discovery')
  for i in range(self.count):
   fields={**self.published,'fo_ver':'2','fo_pub':'1','fo_host':f'UI QA ROOM {i+1:02}','fo_status':'lobby','fo_mode':self.published.get('fo_mode','1'),'fo_track':self.published.get('fo_track','730'),'fo_code':str(94000+i),'fo_lap':'0','fo_laps':self.published.get('fo_laps','3'),'fo_eta_s':'-1','_m':str(1+i%7),'_max':'8'}
   self.rows.append({'id':0xA914000000000001+i,'fields':fields,'active':True,'port':self.game_port+i+1})
  try:
   for row in self.rows:
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    # No SO_REUSEADDR: an occupied port is a fixture error, never a reason to
    # share or steal somebody else's socket.
    try:sock.bind(('0.0.0.0',row['port']))
    except BaseException:sock.close();raise
    sock.setblocking(False);self.sockets.append(sock)
   self.thread=threading.Thread(target=self._run,daemon=True);self.thread.start();return self
  except BaseException:
   for sock in self.sockets:sock.close()
   raise
 def _run(self):
  try:
   while not self.stop.is_set():
    ready,_,_=select.select(self.sockets,[],[],.05)
    for sock in ready:
     data,peer=sock.recvfrom(2048);at=data.find(MAGIC)
     if peer[1]!=self.game_port or peer[0] not in self.addresses or not data or data[0] not in (1,2) or at not in (5,9):
      self.rejected+=1;continue
     with self.lock:
      row=self.rows[self.sockets.index(sock)];fields=dict(row['fields']);active=row['active'];guid=row['id']
     if not active:continue
     body=self.prefix+b'\x1d'+b'\x1e'.join(k.encode()+b'\x1f'+str(v).encode() for k,v in fields.items());assert len(body)<400
     packet=bytes([self.pong])+data[1:at]+guid.to_bytes(8,'big')+MAGIC+body
     sock.sendto(packet,peer);self.replies+=1
  except BaseException as error:self.error=repr(error)
 def change(self,index,**values):
  with self.lock:self.rows[index]['fields'].update({k:str(v) for k,v in values.items()})
 def set_active(self,index,active):
  with self.lock:self.rows[index]['active']=active
 def __exit__(self,*args):
  self.stop.set();self.thread.join(3)
  for sock in self.sockets:sock.close()
  assert not self.thread.is_alive(),'Discovery worker did not stop'
  assert not hasattr(self,'error'),getattr(self,'error',None)
