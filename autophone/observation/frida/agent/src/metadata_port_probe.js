'use strict';

// Diagnostic-only AVFoundation port probe for M0a-2.
// It never invents an input, metadata type, object, or delegate callback.
// Known-invalid addInput/setMetadataObjectTypes calls are skipped so the app
// remains alive long enough to report the topology that caused the crash.
import ObjC from 'frida-objc-bridge';

const events = [];
let installed = false;
let lastSession = null;
let startFigCaptureSession = null;
let startFigCaptureSessionSelector = null;
let configurationCommitted = null;
let configurationCommittedSelector = null;
let repairMissingCommitNotification = false;

function nativeFunction(name, ret, args) {
  return new NativeFunction(Module.getGlobalExportByName(name), ret, args);
}

const objectGetClass = nativeFunction('object_getClass', 'pointer', ['pointer']);
const classGetSuperclass = nativeFunction('class_getSuperclass', 'pointer', ['pointer']);
const classCopyIvarList = nativeFunction(
  'class_copyIvarList', 'pointer', ['pointer', 'pointer']);
const ivarGetName = nativeFunction('ivar_getName', 'pointer', ['pointer']);
const ivarGetTypeEncoding = nativeFunction(
  'ivar_getTypeEncoding', 'pointer', ['pointer']);
const ivarGetOffset = nativeFunction('ivar_getOffset', 'long', ['pointer']);
const freeNative = nativeFunction('free', 'void', ['pointer']);

function emit(event) {
  const row = Object.assign(
    {source: '[DEBUG-m0a-port]', sequence: events.length + 1}, event);
  events.push(row);
  send(row);
}

function descriptions(array) {
  if (array === null || array === undefined) return [];
  const out = [];
  for (let i = 0; i < Number(array.count()); i++) {
    out.push(array.objectAtIndex_(i).toString());
  }
  return out;
}

function describeDevice(device) {
  const result = {};
  if (!device) return result;
  try {
    result.ivars = {};
    for (const [name, value] of Object.entries(device.$ivars)) {
      if (!/metadata/i.test(name)) continue;
      let row = {value: String(value)};
      try {
        if (value && value.$className) {
          row.class_name = value.$className;
        }
      } catch (_) {}
      result.ivars[name] = row;
    }
  } catch (error) {
    result.ivars_error = String(error);
  }
  try {
    result.machine_readable_code_detection_supported =
      Boolean(device.isMachineReadableCodeDetectionSupported());
  } catch (error) {
    result.machine_readable_code_detection_supported_error = String(error);
  }
  for (const selector of [
    'supportedMetadataObjectIdentifiers',
    'availableBoxedMetadataFormatDescriptions',
    'supportedMetadataIdentifiersForFrameRateControl',
  ]) {
    try {
      const value = device[selector]();
      result[selector] = value ? descriptions(value) : [];
    } catch (error) {
      result[selector + '_error'] = String(error);
    }
  }
  return result;
}

function describePorts(input) {
  const out = [];
  try {
    const ports = input.ports();
    for (let i = 0; i < Number(ports.count()); i++) {
      const port = ports.objectAtIndex_(i);
      out.push({
        class_name: port.$className,
        media_type: port.mediaType ? port.mediaType().toString() : null,
        format_description: port.formatDescription && !port.formatDescription().isNull() ?
          port.formatDescription().toString() : null,
        source_device_type: port.sourceDeviceType ? port.sourceDeviceType().toString() : null,
        source_device_position: port.sourceDevicePosition ? Number(port.sourceDevicePosition()) : null,
      });
    }
  } catch (error) {
    out.push({error: String(error)});
  }
  return out;
}

function describeConnections(output) {
  const out = [];
  try {
    const connections = output.connections();
    for (let i = 0; i < Number(connections.count()); i++) {
      const connection = connections.objectAtIndex_(i);
      const ports = connection.inputPorts();
      out.push({
        media_type: connection.mediaType ? connection.mediaType().toString() : null,
        enabled: connection.isEnabled ? Boolean(connection.isEnabled()) : null,
        active: connection.isActive ? Boolean(connection.isActive()) : null,
        input_ports: descriptions(ports),
      });
    }
  } catch (error) {
    out.push({error: String(error)});
  }
  return out;
}

function describeSession(session) {
  const out = {};
  try {
    let current = session;
    for (let depth = 0; current && depth < 8; depth++) {
      for (const [name, value] of Object.entries(current.$ivars)) {
        if (!/error|running|start|fig|session|graph/i.test(name)) continue;
        let text = String(value);
        try { if (value && value.toString) text = value.toString(); } catch (_) {}
        out[name] = text;
      }
      current = current.$super;
    }
  } catch (error) {
    out.error = String(error);
  }
  try {
    let cls = objectGetClass(session.handle);
    const layout = [];
    let internalHandle = null;
    for (let depth = 0; !cls.isNull() && depth < 8; depth++) {
      const countPtr = Memory.alloc(4);
      countPtr.writeU32(0);
      const list = classCopyIvarList(cls, countPtr);
      const count = countPtr.readU32();
      for (let i = 0; i < count; i++) {
        const ivar = list.add(i * Process.pointerSize).readPointer();
        const name = ivarGetName(ivar).readUtf8String();
        const type = ivarGetTypeEncoding(ivar).readUtf8String();
        layout.push(name + ':' + type);
        const address = session.handle.add(Number(ivarGetOffset(ivar)));
        if (name === '_internal' && type[0] === '@') {
          const pointer = address.readPointer();
          if (!pointer.isNull()) internalHandle = pointer;
        }
        if (!/error|running|start|fig|session|graph|config|live/i.test(name)) continue;
        let value;
        if (type[0] === '@') {
          const pointer = address.readPointer();
          value = pointer.isNull() ? 'nil' : new ObjC.Object(pointer).toString();
        } else if (type[0] === 'B' || type[0] === 'c' || type[0] === 'C') {
          value = address.readU8();
        } else if (type[0] === 'i' || type[0] === 'I') {
          value = address.readU32();
        } else if (type[0] === 'q' || type[0] === 'Q') {
          value = address.readU64().toString();
        } else {
          value = address.readPointer().toString();
        }
        out['ivar:' + name] = value;
      }
      if (!list.isNull()) freeNative(list);
      cls = classGetSuperclass(cls);
    }
    out.runtime_ivar_layout = layout;
    if (internalHandle) {
      const internalLayout = [];
      cls = objectGetClass(internalHandle);
      for (let depth = 0; !cls.isNull() && depth < 8; depth++) {
        const countPtr = Memory.alloc(4);
        countPtr.writeU32(0);
        const list = classCopyIvarList(cls, countPtr);
        const count = countPtr.readU32();
        for (let i = 0; i < count; i++) {
          const ivar = list.add(i * Process.pointerSize).readPointer();
          const name = ivarGetName(ivar).readUtf8String();
          const type = ivarGetTypeEncoding(ivar).readUtf8String();
          internalLayout.push(name + ':' + type);
          if (!/error|running|start|fig|session|graph|config|live/i.test(name)) continue;
          const address = internalHandle.add(Number(ivarGetOffset(ivar)));
          let value;
          if (type[0] === '@') {
            const pointer = address.readPointer();
            value = pointer.isNull() ? 'nil' : new ObjC.Object(pointer).toString();
            if (name === 'committedAVCaptureSessionConfigurations' &&
                !pointer.isNull()) {
              const array = new ObjC.Object(pointer);
              const ids = [];
              for (let index = 0; index < Number(array.count()); index++) {
                const config = array.objectAtIndex_(index);
                let id = 'unknown';
                try { id = String(config.configurationID()); } catch (_) {}
                ids.push(id);
              }
              out['internal:committedConfigurationIDs'] = ids;
            }
          } else if (type[0] === 'B' || type[0] === 'c' || type[0] === 'C') {
            value = address.readU8();
          } else if (type[0] === 'i' || type[0] === 'I') {
            value = address.readU32();
          } else if (type[0] === 'q' || type[0] === 'Q') {
            value = address.readU64().toString();
          } else {
            value = address.readPointer().toString();
          }
          out['internal:' + name] = value;
        }
        if (!list.isNull()) freeNative(list);
        cls = classGetSuperclass(cls);
      }
      out.internal_ivar_layout = internalLayout;
    }
  } catch (error) {
    out.runtime_ivars_error = String(error);
  }
  for (const selector of [
    'automaticallyRunsDeferredStart',
    'isManualDeferredStartSupported',
    'isBeingConfigured',
    '_stopError',
    '_figCaptureSession',
    '_figCaptureSessionConfiguration',
  ]) {
    try {
      const value = session[selector]();
      out[selector] = value && value.toString ? value.toString() : String(value);
    } catch (error) {
      out[selector + '_error'] = String(error);
    }
  }
  return out;
}

function readInternalObjectIvar(session, wantedName) {
  let cls = objectGetClass(session.handle);
  let internal = null;
  while (!cls.isNull()) {
    const countPtr = Memory.alloc(4);
    countPtr.writeU32(0);
    const list = classCopyIvarList(cls, countPtr);
    for (let i = 0; i < countPtr.readU32(); i++) {
      const ivar = list.add(i * Process.pointerSize).readPointer();
      if (ivarGetName(ivar).readUtf8String() === '_internal') {
        internal = session.handle.add(Number(ivarGetOffset(ivar))).readPointer();
        break;
      }
    }
    if (!list.isNull()) freeNative(list);
    if (internal && !internal.isNull()) break;
    cls = classGetSuperclass(cls);
  }
  if (!internal || internal.isNull()) return null;
  cls = objectGetClass(internal);
  while (!cls.isNull()) {
    const countPtr = Memory.alloc(4);
    countPtr.writeU32(0);
    const list = classCopyIvarList(cls, countPtr);
    for (let i = 0; i < countPtr.readU32(); i++) {
      const ivar = list.add(i * Process.pointerSize).readPointer();
      if (ivarGetName(ivar).readUtf8String() === wantedName) {
        const value = internal.add(Number(ivarGetOffset(ivar))).readPointer();
        if (!list.isNull()) freeNative(list);
        return value.isNull() ? null : new ObjC.Object(value);
      }
    }
    if (!list.isNull()) freeNative(list);
    cls = classGetSuperclass(cls);
  }
  return null;
}

function install() {
  if (installed || !ObjC.available) return;
  const sessionClass = ObjC.classes.AVCaptureSession;
  const metadataClass = ObjC.classes.AVCaptureMetadataOutput;
  if (!sessionClass || !metadataClass) {
    setTimeout(install, 100);
    return;
  }

  emit({
    event: 'session_diagnostic_methods',
    methods: sessionClass.$methods.filter((name) =>
      /start|running|error|runtime|figcapture/i.test(name)),
  });

  const diagnosticSelectors = new Set([
    '- _handleDidStartRunningNotificationWithPayload:',
    '- _handleDidStopRunningNotificationWithPayload:',
    '- _postRuntimeError:',
    '- _notifyMediaServicesError',
    '- _notifySessionStarted',
    '- _startFigCaptureSession',
    '- _willRunDeferredStart',
    '- _didRunDeferredStart',
    '- runDeferredStartWhenNeeded',
    '- _buildAndRunGraph:',
    '- _makeConfigurationLive:',
    '- _rebuildGraph',
    '- _handleConfigurationCommittedNotificationWithPayload:',
    '- _handleConfigurationDidBecomeLiveNotificationWithPayload:',
    '- _stopError',
  ]);
  try {
    const method = sessionClass['- _startFigCaptureSession'];
    startFigCaptureSession = new NativeFunction(
      method.implementation, 'void', ['pointer', 'pointer']);
    startFigCaptureSessionSelector = method.selector;
  } catch (_) {}
  try {
    const method = sessionClass[
      '- _handleConfigurationCommittedNotificationWithPayload:'];
    configurationCommitted = new NativeFunction(
      method.implementation, 'void', ['pointer', 'pointer', 'pointer']);
    configurationCommittedSelector = method.selector;
  } catch (_) {}
  for (const methodName of sessionClass.$methods.filter((name) =>
    diagnosticSelectors.has(name))) {
    let method;
    try { method = sessionClass[methodName]; } catch (_) { continue; }
    if (!method || !method.implementation) continue;
    emit({event: 'session_diagnostic_hook', method: methodName});
    Interceptor.attach(method.implementation, {
      onEnter(args) {
        this.self = args[0];
        if (repairMissingCommitNotification &&
            methodName ===
              '- _handleConfigurationDidBecomeLiveNotificationWithPayload:') {
          try {
            const session = new ObjC.Object(args[0]);
            const config = readInternalObjectIvar(session, 'sessionConfig');
            const configurationID = config.configurationID();
            const payload = ObjC.classes.NSDictionary.dictionaryWithObject_forKey_(
              ObjC.classes.NSNumber.numberWithLongLong_(configurationID),
              ObjC.classes.NSString.stringWithString_('ConfigurationID'));
            this.repairedLivePayload = payload;
            args[2] = payload.handle;
            emit({
              event: 'session_diagnostic_live_repair',
              configuration_id: String(configurationID),
            });
          } catch (error) {
            emit({event: 'session_diagnostic_live_repair_error', error: String(error)});
          }
        }
        const values = [];
        for (let i = 2; i < Math.min(6, method.argumentTypes.length); i++) {
          let value = args[i].toString();
          try { value = new ObjC.Object(args[i]).toString(); } catch (_) {}
          values.push(value);
        }
        emit({
          event: 'session_diagnostic_callback',
          method: methodName,
          self: args[0].toString(),
          values,
        });
      },
      onLeave(retval) {
        let state;
        if (repairMissingCommitNotification &&
            methodName === '- _buildAndRunGraph:' &&
            !retval.isNull() && configurationCommitted &&
            configurationCommittedSelector) {
          try {
            const session = new ObjC.Object(this.self);
            const config = readInternalObjectIvar(session, 'sessionConfig');
            const configurationID = config.configurationID();
            const payload = ObjC.classes.NSDictionary.dictionaryWithObject_forKey_(
              ObjC.classes.NSNumber.numberWithLongLong_(configurationID),
              ObjC.classes.NSString.stringWithString_('ConfigurationID'));
            configurationCommitted(
              this.self, configurationCommittedSelector, payload.handle);
            emit({
              event: 'session_diagnostic_commit_repair',
              configuration_id: String(configurationID),
            });
          } catch (error) {
            emit({
              event: 'session_diagnostic_commit_repair_error',
              error: String(error),
            });
          }
        }
        if (methodName === '- _handleConfigurationDidBecomeLiveNotificationWithPayload:') {
          try { state = describeSession(new ObjC.Object(this.self)); } catch (_) {}
        }
        emit({
          event: 'session_diagnostic_return',
          method: methodName,
          retval: retval.toString(),
          state,
        });
      },
    });
  }

  const addInput = sessionClass['- addInput:'];
  const addInputOrig = new NativeFunction(
    addInput.implementation, 'void', ['pointer', 'pointer', 'pointer']);
  Interceptor.replace(addInput.implementation, new NativeCallback(
    function (self, sel, inputPtr) {
      const session = new ObjC.Object(self);
      if (inputPtr.isNull()) {
        emit({event: 'add_input', input_null: true, can_add: false, ports: []});
        return;
      }
      const input = new ObjC.Object(inputPtr);
      let device = null;
      try { device = input.device(); } catch (_) {}
      let canAdd = false;
      try { canAdd = Boolean(session.canAddInput_(input)); } catch (_) {}
      emit({
        event: 'add_input',
        session: self.toString(),
        input_null: false,
        input_class: input.$className,
        device_class: device ? device.$className : null,
        device_capabilities: describeDevice(device),
        can_add: canAdd,
        ports: describePorts(input),
      });
      if (canAdd) addInputOrig(self, sel, inputPtr);
    }, 'void', ['pointer', 'pointer', 'pointer']));

  const addOutput = sessionClass['- addOutput:'];
  const addOutputOrig = new NativeFunction(
    addOutput.implementation, 'void', ['pointer', 'pointer', 'pointer']);
  Interceptor.replace(addOutput.implementation, new NativeCallback(
    function (self, sel, outputPtr) {
      const session = new ObjC.Object(self);
      if (outputPtr.isNull()) {
        emit({event: 'add_output', output_null: true, can_add: false});
        return;
      }
      const output = new ObjC.Object(outputPtr);
      let canAdd = false;
      try { canAdd = Boolean(session.canAddOutput_(output)); } catch (_) {}
      if (canAdd) addOutputOrig(self, sel, outputPtr);
      emit({
        event: 'add_output',
        output_null: false,
        output_class: output.$className,
        can_add: canAdd,
        available: output.availableMetadataObjectTypes ?
          descriptions(output.availableMetadataObjectTypes()) : [],
        connections: describeConnections(output),
      });
    }, 'void', ['pointer', 'pointer', 'pointer']));

  const setROI = metadataClass['- setRectOfInterest:'];
  if (setROI && setROI.implementation) {
    Interceptor.attach(setROI.implementation, {
      onEnter(args) {
        // CGRect passed by value; on arm64 it's 4 doubles in args[2..5].
        try {
          const x = args[2].readDouble();
          const y = args[3].readDouble();
          const w = args[4].readDouble();
          const h = args[5].readDouble();
          emit({event: 'rect_of_interest', x, y, w, h});
        } catch (error) {
          emit({event: 'rect_of_interest_error', detail: String(error)});
        }
      },
    });
  }
  const getROI = metadataClass['- rectOfInterest'];

  const setTypes = metadataClass['- setMetadataObjectTypes:'];
  const setTypesOrig = new NativeFunction(
    setTypes.implementation, 'void', ['pointer', 'pointer', 'pointer']);
  Interceptor.replace(setTypes.implementation, new NativeCallback(
    function (self, sel, typesPtr) {
      const output = new ObjC.Object(self);
      const requested = typesPtr.isNull() ? [] : descriptions(new ObjC.Object(typesPtr));
      const available = descriptions(output.availableMetadataObjectTypes());
      const supported = requested.every((item) => available.indexOf(item) !== -1);
      emit({
        event: 'metadata_types', requested, available, supported,
        connections: describeConnections(output),
      });
      if (supported) setTypesOrig(self, sel, typesPtr);
    }, 'void', ['pointer', 'pointer', 'pointer']));

  const startRunning = sessionClass['- startRunning'];
  Interceptor.attach(startRunning.implementation, {
    onEnter(args) {
      this.session = args[0];
      try { lastSession = new ObjC.Object(args[0]); } catch (_) {}
      emit({event: 'start_running_enter', self: args[0].toString()});
    },
    onLeave() {
      let running = null;
      let state = {};
      try {
        const session = new ObjC.Object(this.session);
        running = Boolean(session.isRunning());
        state = describeSession(session);
      } catch (_) {}
      emit({event: 'start_running_leave', running, state});
    },
  });

  for (const selector of ['- beginConfiguration', '- commitConfiguration']) {
    const method = sessionClass[selector];
    if (!method || !method.implementation) continue;
    Interceptor.attach(method.implementation, {
      onEnter() {
        emit({event: selector.slice(2).replace('Configuration', '_configuration')});
      },
    });
  }

  // Observation-only proof of consume: the scanner sets its metadata delegate
  // only when the scan page opens (after this install runs), so hook the setter
  // and attach to whatever delegate class the app actually uses. We only read
  // stringValue; never alter args/return/control flow, never call handleResult:.
  const hookedDelegateSelectors = new Set();
  const cbSel = '- captureOutput:didOutputMetadataObjects:fromConnection:';
  function hookDelegateCallback(delegate) {
    if (!delegate) return;
    const klass = delegate.$class;
    const key = klass.$className;
    if (hookedDelegateSelectors.has(key)) return;
    const cb = klass[cbSel];
    if (!cb || !cb.implementation) {
      emit({event: 'delegate_no_callback', klass: key});
      return;
    }
    hookedDelegateSelectors.add(key);
    Interceptor.attach(cb.implementation, {
      onEnter(args) {
        try {
          const objs = new ObjC.Object(args[3]);
          const values = [];
          for (let i = 0; i < Number(objs.count()); i++) {
            const o = objs.objectAtIndex_(i);
            let sv = null;
            try { sv = o.stringValue ? o.stringValue().toString() : null; } catch (_) {}
            values.push({type: o.$className, stringValue: sv});
          }
          emit({event: 'decoded', count: values.length, objects: values});
        } catch (error) {
          emit({event: 'decoded_error', detail: String(error)});
        }
      },
    });
    emit({event: 'delegate_hooked', klass: key});
  }
  const setDelegate = metadataClass['- setMetadataObjectsDelegate:queue:'];
  if (setDelegate && setDelegate.implementation) {
    Interceptor.attach(setDelegate.implementation, {
      onEnter(args) {
        try {
          if (!args[2].isNull()) hookDelegateCallback(new ObjC.Object(args[2]));
        } catch (error) {
          emit({event: 'delegate_setter_error', detail: String(error)});
        }
      },
    });
    emit({event: 'delegate_setter_hooked'});
  }

  installed = true;
  emit({event: 'installed'});
}

rpc.exports = {
  snapshot() { return {installed, events}; },
  forceStart() {
    if (!lastSession) return {ok: false, error: 'no session'};
    try {
      if (!startFigCaptureSession || !startFigCaptureSessionSelector) {
        return {ok: false, error: 'start method unavailable'};
      }
      startFigCaptureSession(lastSession.handle, startFigCaptureSessionSelector);
      return {ok: true};
    } catch (error) {
      return {ok: false, error: String(error)};
    }
  },
  startAgain() {
    if (!lastSession) return {ok: false, error: 'no session'};
    try {
      lastSession.startRunning();
      return {ok: true, running: Boolean(lastSession.isRunning())};
    } catch (error) {
      return {ok: false, error: String(error)};
    }
  },
  stopStart() {
    if (!lastSession) return {ok: false, error: 'no session'};
    try {
      lastSession.stopRunning();
      lastSession.startRunning();
      return {ok: true, running: Boolean(lastSession.isRunning())};
    } catch (error) {
      return {ok: false, error: String(error)};
    }
  },
  enableCommitRepair() {
    repairMissingCommitNotification = true;
    return {ok: Boolean(configurationCommitted)};
  },
};

install();
