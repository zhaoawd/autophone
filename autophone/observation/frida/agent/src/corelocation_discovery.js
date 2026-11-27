'use strict';
// Phase 5 M0a — CoreLocation discovery（guest 侧，编译后由 python spawn-gate 加载）。
//
// 目标是钉住「App 定位能否走 Frida 进程内 mock」之前的只读事实：
//   1. CoreLocation 关键类是否在 App 进程内可解。
//   2. CLLocationManager 关键方法能否被 hook。
//   3. 冷启动/登录页阶段 App 是否触发定位、授权、delegate 设置或读取当前位置。
//
// 本 agent 只观察，不返回伪造坐标、不改 delegate、不拦截放行。

import ObjC from 'frida-objc-bridge';

const C = ObjC.classes;

function ptrStr(p) {
  try { return p ? p.toString() : null; } catch (e) { return null; }
}

function objClassName(p) {
  try {
    if (!p || p.isNull()) return null;
    return new ObjC.Object(p).$className;
  } catch (e) { return null; }
}

function objDesc(p) {
  try {
    if (!p || p.isNull()) return null;
    return new ObjC.Object(p).toString();
  } catch (e) { return null; }
}

function boolArg(p) {
  try { return !p.isNull(); } catch (e) { return null; }
}

function locationSnapshot(location) {
  if (!location || (location.isNull && location.isNull())) return null;
  try {
    var coordinate = location.coordinate();
    return { latitude: Number(coordinate.latitude === undefined ? coordinate[0] : coordinate.latitude), longitude: Number(coordinate.longitude === undefined ? coordinate[1] : coordinate.longitude),
      horizontal_accuracy_m: Number(location.horizontalAccuracy()),
      timestamp_s: Number(location.timestamp().timeIntervalSince1970()),
      speed_mps: Number(location.speed()), course_deg: Number(location.course()) };
  } catch (e) { return null; }
}

var probeReadDepth = 0;
var observedDelegates = new Set();
function observeDelegate(delegatePtr) {
  try {
    if (!delegatePtr || delegatePtr.isNull()) return;
    var delegate = new ObjC.Object(delegatePtr);
    var method = delegate['- locationManager:didUpdateLocations:'];
    if (!method) return;
    var key = method.implementation.toString();
    if (observedDelegates.has(key)) return;
    observedDelegates.add(key);
    Interceptor.attach(method.implementation, {
      onEnter: function (args) {
        try {
          var locations = new ObjC.Object(args[3]);
          send({ ev: 'cl_delegate_update', manager_ptr: ptrStr(args[2]),
            delegate_cls: objClassName(args[0]), location: locationSnapshot(locations.lastObject()),
            pid: Process.id, ts: Date.now() });
        } catch (e) { send({ ev: 'cl_delegate_error', err: '' + e, ts: Date.now() }); }
      }
    });
  } catch (e) { send({ ev: 'cl_delegate_hook_error', err: '' + e, ts: Date.now() }); }
}

function managerSnapshot(managerPtr) {
  var out = { manager_ptr: ptrStr(managerPtr) };
  try {
    var manager = new ObjC.Object(managerPtr);
    try { out.delegate_cls = objClassName(manager.delegate()); } catch (e) {}
    try {
      probeReadDepth += 1;
      var location = manager.location();
      out.location_desc = objDesc(location);
      out.location = locationSnapshot(location);
    } catch (e) {} finally { probeReadDepth -= 1; }
    try { out.desired_accuracy = '' + manager.desiredAccuracy(); } catch (e) {}
    try { out.distance_filter = '' + manager.distanceFilter(); } catch (e) {}
    try { out.background_updates = '' + manager.allowsBackgroundLocationUpdates(); } catch (e) {}
  } catch (e) {
    out.err = '' + e;
  }
  return out;
}

function hookInstance(clsName, sel, handler) {
  try {
    var cls = C[clsName];
    var m = cls ? cls[sel] : null;
    if (!m) {
      send({ ev: 'hook_missing', cls: clsName, sel: sel });
      return;
    }
    Interceptor.attach(m.implementation, handler);
    send({ ev: 'hook_ok', cls: clsName, sel: sel });
  } catch (e) {
    send({ ev: 'hook_err', cls: clsName, sel: sel, err: '' + e });
  }
}

function hookClass(clsName, sel, handler) {
  try {
    var cls = C[clsName];
    var m = cls ? cls[sel] : null;
    if (!m) {
      send({ ev: 'hook_missing', cls: clsName, sel: sel });
      return;
    }
    Interceptor.attach(m.implementation, handler);
    send({ ev: 'hook_ok', cls: clsName, sel: sel });
  } catch (e) {
    send({ ev: 'hook_err', cls: clsName, sel: sel, err: '' + e });
  }
}

function callEvent(sel, managerPtr, extra) {
  var payload = {
    ev: 'cl_call',
    cls: 'CLLocationManager',
    sel: sel,
    ts: Date.now()
  };
  var snap = managerSnapshot(managerPtr);
  Object.keys(snap).forEach(function (k) { payload[k] = snap[k]; });
  if (extra) Object.keys(extra).forEach(function (k) { payload[k] = extra[k]; });
  send(payload);
}

send({
  ev: 'surface',
  objc_available: ObjC.available === true,
  classes: {
    CLLocationManager: !!C.CLLocationManager,
    CLLocation: !!C.CLLocation,
    CLGeocoder: !!C.CLGeocoder,
    CLCircularRegion: !!C.CLCircularRegion
  },
  primitives: {
    Interceptor: typeof Interceptor !== 'undefined',
    Module: typeof Module !== 'undefined',
    CModule: typeof CModule !== 'undefined'
  },
  ts: Date.now()
});

[
  '- init',
  '- setDelegate:',
  '- requestWhenInUseAuthorization',
  '- requestAlwaysAuthorization',
  '- requestLocation',
  '- startUpdatingLocation',
  '- stopUpdatingLocation',
  '- startMonitoringSignificantLocationChanges',
  '- stopMonitoringSignificantLocationChanges',
  '- startUpdatingHeading',
  '- stopUpdatingHeading',
  '- location',
  '- setAllowsBackgroundLocationUpdates:',
  '- setPausesLocationUpdatesAutomatically:'
].forEach(function (sel) {
  hookInstance('CLLocationManager', sel, {
    onEnter: function (args) {
      var extra = {};
      if (sel === '- setDelegate:') {
        extra.new_delegate_cls = objClassName(args[2]);
        observeDelegate(args[2]);
      }
      if (sel === '- setAllowsBackgroundLocationUpdates:') extra.value = boolArg(args[2]);
      if (sel === '- setPausesLocationUpdatesAutomatically:') extra.value = boolArg(args[2]);
      callEvent(sel, args[0], extra);
    },
    onLeave: function (retval) {
      if (sel === '- location') {
        send({
          ev: 'cl_location_return',
          sel: sel,
          location_ptr: ptrStr(retval),
          location_desc: objDesc(retval),
          location: retval.isNull() ? null : locationSnapshot(new ObjC.Object(retval)),
          probe_read: probeReadDepth > 0, pid: Process.id,
          ts: Date.now()
        });
      }
    }
  });
});

[
  '+ authorizationStatus',
  '+ locationServicesEnabled',
  '+ significantLocationChangeMonitoringAvailable',
  '+ headingAvailable'
].forEach(function (sel) {
  hookClass('CLLocationManager', sel, {
    onEnter: function () {
      send({ ev: 'cl_class_call', cls: 'CLLocationManager', sel: sel, ts: Date.now() });
    },
    onLeave: function (retval) {
      send({
        ev: 'cl_class_return',
        cls: 'CLLocationManager',
        sel: sel,
        retval: ptrStr(retval),
        ts: Date.now()
      });
    }
  });
});

rpc.exports = {
  ping: function () { return 'pong'; },
  guestNowMs: function () { return Date.now(); },
  managerSnapshots: function () {
    const snapshots = [];
    try {
      ObjC.chooseSync(C.CLLocationManager).forEach(function (manager) {
        snapshots.push(managerSnapshot(manager.handle));
      });
    } catch (e) {
      send({ ev: 'manager_readback_error', err: '' + e, ts: Date.now() });
    }
    return snapshots;
  }
};
send({ ev: 'loaded', ts: Date.now() });
