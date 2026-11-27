'use strict';
// M0 scanner consume probe. Observation-only: this hook reads metadata object
// stringValue values and never changes arguments/return values or calls app code.

import ObjC from 'frida-objc-bridge';

const CLASS_NAME = 'SKScanScheduleVC';
const SELECTOR = '- captureOutput:didOutputMetadataObjects:fromConnection:';

function emit(payload) {
  send(Object.assign({ source: 'scanner_observe' }, payload));
}

function install() {
  if (!ObjC.available) {
    emit({ event: 'error', error: 'objc_unavailable' });
    return;
  }
  const klass = ObjC.classes[CLASS_NAME];
  if (!klass || !klass[SELECTOR]) {
    emit({ event: 'error', error: 'selector_missing', class_name: CLASS_NAME,
           selector: SELECTOR });
    return;
  }

  Interceptor.attach(klass[SELECTOR].implementation, {
    onEnter(args) {
      try {
        const objects = new ObjC.Object(args[3]);
        const count = Number(objects.count());
        for (let i = 0; i < count; i++) {
          const object = objects.objectAtIndex_(i);
          try {
            const value = object.stringValue();
            if (value !== null) {
              emit({ event: 'decoded', payload: value.toString(), index: i,
                     metadata_count: count });
            }
          } catch (_) {
            // Not every AVMetadataObject subtype has stringValue.
          }
        }
      } catch (error) {
        emit({ event: 'error', error: 'read_failed', detail: String(error) });
      }
    }
  });
  emit({ event: 'installed', class_name: CLASS_NAME, selector: SELECTOR });
}

install();
