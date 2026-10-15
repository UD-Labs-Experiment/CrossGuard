"""Read DXGI adapter identities for explicit DirectML GPU selection on Windows."""
import ctypes as C
import uuid

def adapters():
 class GUID(C.Structure):_fields_=[('bytes',C.c_ubyte*16)]
 class LUID(C.Structure):_fields_=[('LowPart',C.c_uint32),('HighPart',C.c_int32)]
 class DESC(C.Structure):
  _fields_=[('Description',C.c_wchar*128),('VendorId',C.c_uint32),('DeviceId',C.c_uint32),('SubSysId',C.c_uint32),('Revision',C.c_uint32),('DedicatedVideoMemory',C.c_size_t),('DedicatedSystemMemory',C.c_size_t),('SharedSystemMemory',C.c_size_t),('AdapterLuid',LUID),('Flags',C.c_uint32)]
 def method(ptr,index,result,*args):
  vtable=C.cast(ptr,C.POINTER(C.POINTER(C.c_void_p))).contents
  return C.WINFUNCTYPE(result,C.c_void_p,*args)(vtable[index])
 factory=C.c_void_p();iid=GUID((C.c_ubyte*16).from_buffer_copy(uuid.UUID('770aae78-f26f-4dba-a829-253c83d1b387').bytes_le))
 create=C.WinDLL('dxgi').CreateDXGIFactory1;create.argtypes=[C.POINTER(GUID),C.POINTER(C.c_void_p)];create.restype=C.c_int32
 if create(C.byref(iid),C.byref(factory))<0:raise RuntimeError('DXGI factory unavailable')
 result=[]
 try:
  for index in range(16):
   adapter=C.c_void_p()
   hr=method(factory,12,C.c_int32,C.c_uint32,C.POINTER(C.c_void_p))(factory,index,C.byref(adapter))
   if hr<0:break
   try:
    desc=DESC();hr=method(adapter,10,C.c_int32,C.POINTER(DESC))(adapter,C.byref(desc))
    if hr<0:raise RuntimeError('DXGI adapter description failed')
    result.append({'device_id':index,'name':desc.Description,'vendor_id':desc.VendorId,'dedicated_vram_bytes':desc.DedicatedVideoMemory,'software':bool(desc.Flags&2)})
   finally:method(adapter,2,C.c_uint32)(adapter)
 finally:method(factory,2,C.c_uint32)(factory)
 return result

def nvidia_adapter():
 choices=[a for a in adapters() if a['vendor_id']==0x10de and not a['software']]
 if not choices:raise RuntimeError('Requested NVIDIA GPU is not available; no CPU fallback')
 return max(choices,key=lambda a:a['dedicated_vram_bytes'])

if __name__=='__main__':
 import json
 print(json.dumps(adapters(),indent=2))
