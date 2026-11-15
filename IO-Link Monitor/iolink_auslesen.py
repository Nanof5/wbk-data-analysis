import json, urllib.request
M="http://169.254.33.228/"
def req(adr, data=None):
    b={"code":"request","cid":1,"adr":adr}
    if data is not None: b["data"]=data
    r=urllib.request.urlopen(urllib.request.Request(M,json.dumps(b).encode(),{"Content-Type":"application/json"}),timeout=5).read()
    return json.loads(r)
def multi(paths):
    d=req("/getdatamulti",{"datatosend":list(paths)})["data"]
    return {k:(v.get("data") if v.get("code")==200 else f"<{v.get('code')}>") for k,v in d.items()}
info=multi([f"/deviceinfo/{k}" for k in "vendor productcode serialnumber swrevision hwrevision fieldbustype".split()]
           +["/fieldbussetup/network/ipaddress","/fieldbussetup/network/subnetmask","/fieldbussetup/hostname","/fieldbussetup/connectionstatus"])
for k,v in info.items(): print(f"{k:42} {v}")
print()
MODES={0:"deaktiviert",1:"DI",2:"DO",3:"IO-Link"}
for p in range(1,9):
    b=f"/iolinkmaster/port[{p}]"
    r=multi([b+"/mode",b+"/iolinkdevice/status",b+"/iolinkdevice/productname",b+"/iolinkdevice/vendorid",b+"/iolinkdevice/deviceid",b+"/iolinkdevice/serial",b+"/iolinkdevice/pdin",b+"/pin2in"])
    v=list(r.values())
    if all(isinstance(x,str) and x.startswith("<") for x in v): continue
    print(f"Port {p}: mode={MODES.get(v[0],v[0])} status={v[1]} name={v[2]} vendor={v[3]} device={v[4]} serial={v[5]} pdin={v[6]} pin2={v[7]}")
