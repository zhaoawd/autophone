'use strict';
// CoreLocation control probe（guest 侧）。
//
// This agent is intentionally opt-in. It only returns/injects a mocked
// CLLocation after the host calls rpc.configure(...). The goal is to prove
// whether app location logic can be controlled through the in-process
// CoreLocation surface.

import ObjC from 'frida-objc-bridge';

const C = ObjC.classes;

let config = {
  enabled: false,
  sampleSeq: 0,
  latitude: null,
  longitude: null,
  altitude: 0,
  horizontalAccuracy: 15,
  verticalAccuracy: 5,
  course: -1,
  speed: -1,
  timestampMs: null,
  injectDelegate: true,
  injectDelayMs: 100
};
let mockLocation = null;
const knownManagers = {};

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

function makeLocation() {
  if (!C.CLLocation || config.latitude === null || config.longitude === null) return null;
  try {
    const loc = C.CLLocation.alloc();
    const selector =
      'initWithCoordinate:altitude:horizontalAccuracy:verticalAccuracy:course:speed:timestamp:';
    if (!loc.respondsToSelector_(ObjC.selector(selector))) {
      send({ ev: 'mock_location_error', err: 'full CLLocation initializer unavailable', ts: Date.now() });
      return null;
    }
    const timestampMs = Number(config.timestampMs || Date.now());
    const timestamp = C.NSDate.dateWithTimeIntervalSince1970_(timestampMs / 1000);
    mockLocation =
      loc.initWithCoordinate_altitude_horizontalAccuracy_verticalAccuracy_course_speed_timestamp_(
        [Number(config.latitude), Number(config.longitude)],
        Number(config.altitude),
        Number(config.horizontalAccuracy),
        Number(config.verticalAccuracy),
        Number(config.course),
        Number(config.speed),
        timestamp
      );
    return mockLocation;
  } catch (e) {
    send({ ev: 'mock_location_error', err: '' + e, ts: Date.now() });
    return null;
  }
}

function deliveredEvidence(extra) {
  const out = {
    sampleSeq: Number(config.sampleSeq),
    latitude: Number(config.latitude),
    longitude: Number(config.longitude),
    altitude: Number(config.altitude),
    horizontalAccuracy: Number(config.horizontalAccuracy),
    verticalAccuracy: Number(config.verticalAccuracy),
    course: Number(config.course),
    speed: Number(config.speed),
    timestampMs: Number(config.timestampMs),
    ts: Date.now()
  };
  if (extra) Object.keys(extra).forEach(function (key) { out[key] = extra[key]; });
  return out;
}

function rememberManager(managerPtr) {
  const key = ptrStr(managerPtr);
  if (key) knownManagers[key] = managerPtr;
}

function currentMockLocation() {
  if (!config.enabled) return null;
  if (mockLocation === null) return makeLocation();
  return mockLocation;
}

function managerSnapshot(managerPtr, includeLocation) {
  var out = { manager_ptr: ptrStr(managerPtr) };
  try {
    var manager = new ObjC.Object(managerPtr);
    try { out.delegate_cls = objClassName(manager.delegate()); } catch (e) {}
    if (includeLocation) {
      try { out.location_desc = objDesc(manager.location()); } catch (e) {}
    }
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

function callEvent(sel, managerPtr, extra) {
  rememberManager(managerPtr);
  var payload = {
    ev: 'cl_call',
    cls: 'CLLocationManager',
    sel: sel,
    control_enabled: config.enabled === true,
    ts: Date.now()
  };
  var snap = managerSnapshot(managerPtr, sel !== '- location');
  Object.keys(snap).forEach(function (k) { payload[k] = snap[k]; });
  if (extra) Object.keys(extra).forEach(function (k) { payload[k] = extra[k]; });
  send(payload);
}

function deliverToManager(managerPtr, reason) {
  if (!config.enabled || !config.injectDelegate) return false;
  try {
    rememberManager(managerPtr);
    var loc = currentMockLocation();
    if (loc === null) {
      send({ ev: 'delegate_inject_skipped', reason: 'no_mock_location', ts: Date.now() });
      return false;
    }
    var manager = new ObjC.Object(managerPtr);
    var delegate = manager.delegate();
    if (!delegate || delegate.isNull()) {
      send({ ev: 'delegate_inject_skipped', reason: 'no_delegate', source: reason, ts: Date.now() });
      return false;
    }
    var delegateObj = new ObjC.Object(delegate);
    var sel = '- locationManager:didUpdateLocations:';
    if (!delegateObj.respondsToSelector_(ObjC.selector('locationManager:didUpdateLocations:'))) {
      send({
        ev: 'delegate_inject_skipped',
        reason: 'selector_missing',
        delegate_cls: delegateObj.$className,
        source: reason,
        ts: Date.now()
      });
      return false;
    }
    var locations = C.NSArray.arrayWithObject_(loc);
    delegateObj[sel](manager, locations);
    send({
      ev: 'delegate_injected',
      delegate_cls: delegateObj.$className,
      source: reason,
      latitude: config.latitude,
      longitude: config.longitude,
      sampleSeq: config.sampleSeq,
      ts: Date.now()
    });
    send(deliveredEvidence({
      ev: 'location_consumed',
      via: 'delegate',
      source: reason,
      delegate_cls: delegateObj.$className
    }));
    return true;
  } catch (e) {
    send({ ev: 'delegate_inject_error', source: reason, err: '' + e, ts: Date.now() });
    return false;
  }
}

function injectDelegateUpdate(managerPtr, reason) {
  if (!config.enabled || !config.injectDelegate) return;
  setTimeout(function () {
    try {
      ObjC.schedule(ObjC.mainQueue, function () {
        deliverToManager(managerPtr, reason);
      });
    } catch (e) {
      send({ ev: 'delegate_schedule_error', source: reason, err: '' + e, ts: Date.now() });
    }
  }, Number(config.injectDelayMs || 0));
}

function scheduleDelivery(managerPtr, reason) {
  try {
    ObjC.schedule(ObjC.mainQueue, function () {
      deliverToManager(managerPtr, reason);
    });
    return true;
  } catch (e) {
    send({ ev: 'delegate_schedule_error', source: reason, err: '' + e, ts: Date.now() });
    return false;
  }
}

send({
  ev: 'surface',
  objc_available: ObjC.available === true,
  classes: {
    CLLocationManager: !!C.CLLocationManager,
    CLLocation: !!C.CLLocation,
    NSArray: !!C.NSArray
  },
  primitives: {
    Interceptor: typeof Interceptor !== 'undefined',
    ObjC: ObjC.available === true
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
  '- location'
].forEach(function (sel) {
  hookInstance('CLLocationManager', sel, {
    onEnter: function (args) {
      this.managerPtr = args[0];
      var extra = {};
      if (sel === '- setDelegate:') extra.new_delegate_cls = objClassName(args[2]);
      callEvent(sel, args[0], extra);
      if (sel === '- requestLocation' || sel === '- startUpdatingLocation') {
        injectDelegateUpdate(args[0], sel);
      }
    },
    onLeave: function (retval) {
      if (sel !== '- location') return;
      var before = objDesc(retval);
      var loc = currentMockLocation();
      if (loc !== null) {
        retval.replace(loc.handle);
        send({
          ev: 'location_return_replaced',
          original_desc: before,
          replacement_desc: objDesc(loc.handle),
          latitude: config.latitude,
          longitude: config.longitude,
          ts: Date.now()
        });
        send(deliveredEvidence({
          ev: 'location_consumed',
          via: 'location_getter',
          manager_ptr: ptrStr(this.managerPtr)
        }));
      } else {
        send({ ev: 'location_return_observed', original_desc: before, ts: Date.now() });
      }
    }
  });
});

// Do not intercept CLLocationManager class methods such as
// +authorizationStatus. Some map SDKs crash with EXC_BAD_ACCESS while
// materializing their first scene when those observation-only hooks are active.
// Instance hooks provide the control and consumption evidence required by the
// adapter without changing class-level availability or authorization calls.
rpc.exports = {
  ping: function () { return 'pong'; },
  configure: function (opts) {
    opts = opts || {};
    config.enabled = opts.enabled !== false;
    config.sampleSeq = Number(opts.sampleSeq === undefined ? config.sampleSeq : opts.sampleSeq);
    config.latitude = Number(opts.latitude);
    config.longitude = Number(opts.longitude);
    config.altitude = Number(opts.altitude === undefined ? config.altitude : opts.altitude);
    config.horizontalAccuracy = Number(opts.horizontalAccuracy || config.horizontalAccuracy);
    config.verticalAccuracy = Number(
      opts.verticalAccuracy === undefined ? config.verticalAccuracy : opts.verticalAccuracy
    );
    config.course = Number(opts.course === undefined ? config.course : opts.course);
    config.speed = Number(opts.speed === undefined ? config.speed : opts.speed);
    config.timestampMs = Number(
      opts.timestampMs === undefined ? Date.now() : opts.timestampMs
    );
    config.injectDelegate = opts.injectDelegate !== false;
    config.injectDelayMs = Number(opts.injectDelayMs || config.injectDelayMs);
    mockLocation = null;
    send({
      ev: 'control_configured',
      enabled: config.enabled,
      sampleSeq: config.sampleSeq,
      latitude: config.latitude,
      longitude: config.longitude,
      altitude: config.altitude,
      horizontalAccuracy: config.horizontalAccuracy,
      verticalAccuracy: config.verticalAccuracy,
      course: config.course,
      speed: config.speed,
      timestampMs: config.timestampMs,
      injectDelegate: config.injectDelegate,
      ts: Date.now()
    });
    return {
      enabled: config.enabled,
      sampleSeq: config.sampleSeq,
      latitude: config.latitude,
      longitude: config.longitude,
      altitude: config.altitude,
      horizontalAccuracy: config.horizontalAccuracy,
      verticalAccuracy: config.verticalAccuracy,
      course: config.course,
      speed: config.speed,
      timestampMs: config.timestampMs,
      lazyMockLocation: true
    };
  },
  pushNow: function () {
    try {
      ObjC.chooseSync(C.CLLocationManager).forEach(function (manager) {
        rememberManager(manager.handle);
      });
    } catch (e) {
      send({ ev: 'manager_enumeration_error', err: '' + e, ts: Date.now() });
    }
    const keys = Object.keys(knownManagers);
    let scheduled = 0;
    keys.forEach(function (key) {
      if (scheduleDelivery(knownManagers[key], 'pushNow')) scheduled += 1;
    });
    return {
      sampleSeq: config.sampleSeq,
      managers: keys.length,
      scheduled: scheduled,
      delivered: 0
    };
  }
};

send({ ev: 'loaded', ts: Date.now() });
