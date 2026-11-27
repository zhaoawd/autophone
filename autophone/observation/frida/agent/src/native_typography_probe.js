'use strict';

// Local-only, read-only UIKit typography probe.
//
// This agent deliberately observes the resolved native view tree.  It does not
// hook setters, mutate a view, or read React Native's Shadow Tree.  A stable_id
// resolves only when exactly one accessibility wrapper and exactly one native
// text descendant are present.  Missing and ambiguous mappings remain explicit.

import ObjC from 'frida-objc-bridge';

const MAX_DEPTH = 64;
const MAX_VIEWS = 20000;

function text(value) {
  if (value === null || value === undefined) return null;
  try { return String(value); } catch (_) { return null; }
}

function number(value) {
  try {
    const result = Number(value);
    return Number.isFinite(result) ? result : null;
  } catch (_) { return null; }
}

function point(value) {
  try {
    if (value && value.x !== undefined) {
      return {x: number(value.x), y: number(value.y)};
    }
    return {x: number(value[0]), y: number(value[1])};
  } catch (_) { return {x: null, y: null}; }
}

function rect(value) {
  try {
    if (value && value.origin !== undefined) {
      return {
        x: number(value.origin.x), y: number(value.origin.y),
        width: number(value.size.width), height: number(value.size.height),
      };
    }
    const origin = point(value[0]);
    const size = point(value[1]);
    return {x: origin.x, y: origin.y, width: size.x, height: size.y};
  } catch (_) {
    return {x: null, y: null, width: null, height: null};
  }
}

function responds(object, selector) {
  try {
    const property = String(selector).replace(/:/g, '_');
    if (typeof object[property] === 'function') return true;
  } catch (_) {}
  try { return Boolean(object.respondsToSelector_(selector)); }
  catch (_) { return false; }
}

function arrayItems(array) {
  const result = [];
  if (!array) return result;
  try {
    const count = Math.min(number(array.count()) || 0, MAX_VIEWS);
    for (let index = 0; index < count; index++) {
      result.push(array.objectAtIndex_(index));
    }
  } catch (_) {}
  return result;
}

function identifier(view) {
  if (!responds(view, 'accessibilityIdentifier')) return null;
  try { return text(view.accessibilityIdentifier()); }
  catch (_) { return null; }
}

function viewText(view) {
  try {
    if (responds(view, 'text')) {
      const value = text(view.text());
      if (value !== null && value.length > 0) return value;
    }
  } catch (_) {}
  try {
    if (responds(view, 'attributedText')) {
      const attributed = view.attributedText();
      if (attributed && responds(attributed, 'string')) {
        const value = text(attributed.string());
        if (value !== null && value.length > 0) return value;
      }
    }
  } catch (_) {}
  return null;
}

function windowFrame(view) {
  try {
    if (responds(view, 'convertRect:toView:')) {
      return rect(view.convertRect_toView_(view.bounds(), null));
    }
  } catch (_) {}
  try { return rect(view.frame()); }
  catch (_) { return rect(null); }
}

function descriptorTraits(font) {
  const result = {symbolic_traits: null, effective_weight: null};
  try {
    const descriptor = font.fontDescriptor();
    if (!descriptor) return result;
    if (responds(descriptor, 'symbolicTraits')) {
      result.symbolic_traits = number(descriptor.symbolicTraits());
    }
    if (responds(descriptor, 'objectForKey:')) {
      const traits = descriptor.objectForKey_('NSCTFontTraitsAttribute');
      if (traits && responds(traits, 'objectForKey:')) {
        result.effective_weight = number(
          traits.objectForKey_('NSCTFontWeightTrait'));
      }
    }
  } catch (error) {
    result.error = text(error);
  }
  return result;
}

function lineGeometry(view) {
  const result = {
    line_fragment_status: 'unsupported',
    line_fragment_bounds: [],
    text_bounds: null,
  };
  try {
    if (responds(view, 'textRectForBounds:limitedToNumberOfLines:')) {
      const lines = responds(view, 'numberOfLines') ? number(view.numberOfLines()) : 0;
      result.text_bounds = rect(
        view.textRectForBounds_limitedToNumberOfLines_(view.bounds(), lines || 0));
    }
  } catch (error) {
    result.text_bounds_error = text(error);
  }
  // UITextView exposes its live TextKit layout.  UILabel's textRect is retained
  // separately and is not mislabeled as line-fragment evidence.
  try {
    if (!responds(view, 'layoutManager') || !responds(view, 'textContainer')) {
      return result;
    }
    const manager = view.layoutManager();
    const container = view.textContainer();
    if (!manager || !container || !responds(manager, 'numberOfGlyphs')) return result;
    const count = number(manager.numberOfGlyphs()) || 0;
    if (count === 0) {
      result.line_fragment_status = 'observed';
      return result;
    }
    let glyph = 0;
    const ranges = [];
    while (glyph < count && ranges.length < 1000) {
      const effectiveRange = Memory.alloc(Process.pointerSize * 2);
      effectiveRange.writeULong(glyph);
      effectiveRange.add(Process.pointerSize).writeULong(0);
      const fragment = manager.lineFragmentUsedRectForGlyphAtIndex_effectiveRange_(
        glyph, effectiveRange);
      const location = number(effectiveRange.readULong());
      const length = number(effectiveRange.add(Process.pointerSize).readULong());
      if (location === null || length === null || length <= 0) {
        result.line_fragment_status = 'failed';
        result.line_fragment_error = 'invalid_effective_range';
        return result;
      }
      ranges.push({glyph_location: location, glyph_length: length, bounds: rect(fragment)});
      glyph = location + length;
    }
    result.line_fragment_bounds = ranges;
    result.line_fragment_status = glyph >= count ? 'observed' : 'failed';
    if (glyph < count) result.line_fragment_error = 'line_fragment_limit_exceeded';
  } catch (error) {
    result.line_fragment_status = 'failed';
    result.line_fragment_error = text(error);
  }
  return result;
}

function attributedRuns(view) {
  const result = {text: null, runs: [], failure: null, diagnostic: {}};
  try {
    if (!responds(view, 'attributedText')) {
      result.failure = 'attributed_text_selector_unavailable';
      return result;
    }
    let attributed = view.attributedText();
    result.diagnostic.raw_type = typeof attributed;
    result.diagnostic.raw_value = text(attributed);
    result.diagnostic.raw_class = attributed && attributed.$className
      ? text(attributed.$className) : null;
    // Release binaries can strip the ObjC object return encoding down to a
    // pointer.  RCTParagraphComponentView's declared API returns an
    // NSAttributedString; wrap that pointer without touching its C++ state.
    if (attributed && !attributed.$className) {
      try { attributed = new ObjC.Object(attributed); }
      catch (error) {
        result.failure = 'attributed_text_pointer_wrap_failed: ' + text(error);
        return result;
      }
    }
    result.diagnostic.wrapped_class = attributed && attributed.$className
      ? text(attributed.$className) : null;
    if (!attributed || !responds(attributed, 'string')) {
      result.failure = 'attributed_text_not_ns_attributed_string';
      return result;
    }
    result.text = text(attributed.string());
    const length = responds(attributed, 'length') ? number(attributed.length()) : 0;
    let index = 0;
    while (index < length && result.runs.length < 1000) {
      const effectiveRange = Memory.alloc(Process.pointerSize * 2);
      effectiveRange.writeULong(index);
      effectiveRange.add(Process.pointerSize).writeULong(0);
      const attributes = attributed.attributesAtIndex_effectiveRange_(index, effectiveRange);
      const location = number(effectiveRange.readULong());
      const runLength = number(effectiveRange.add(Process.pointerSize).readULong());
      if (location === null || runLength === null || runLength <= 0) {
        result.failure = 'invalid_attributed_range';
        break;
      }
      const run = {
        location: location, length: runLength, attributes: [],
        font: null, paragraph_style: null,
      };
      for (const key of arrayItems(attributes.allKeys ? attributes.allKeys() : null)) {
        const keyName = text(key);
        let value = null;
        try { value = attributes.objectForKey_(key); } catch (_) {}
        const valueClass = value && value.$className ? text(value.$className) : null;
        run.attributes.push({key: keyName, value_class: valueClass});
        if (value && responds(value, 'fontName') && responds(value, 'pointSize')) {
          const traits = descriptorTraits(value);
          run.font = Object.assign({
            resolved_font_name: text(value.fontName()),
            resolved_family_name: responds(value, 'familyName') ? text(value.familyName()) : null,
            point_size: number(value.pointSize()),
          }, traits);
        }
        if (value && (
          valueClass === 'NSParagraphStyle' ||
          valueClass === 'NSMutableParagraphStyle' ||
          responds(value, 'lineHeightMultiple')
        )) {
          run.paragraph_style = {
            line_spacing: responds(value, 'lineSpacing') ? number(value.lineSpacing()) : null,
            paragraph_spacing: responds(value, 'paragraphSpacing')
              ? number(value.paragraphSpacing()) : null,
            minimum_line_height: responds(value, 'minimumLineHeight')
              ? number(value.minimumLineHeight()) : null,
            maximum_line_height: responds(value, 'maximumLineHeight')
              ? number(value.maximumLineHeight()) : null,
            line_height_multiple: responds(value, 'lineHeightMultiple')
              ? number(value.lineHeightMultiple()) : null,
          };
        }
      }
      run.attributes.sort((left, right) => String(left.key).localeCompare(String(right.key)));
      result.runs.push(run);
      index = location + runLength;
    }
    if (index < length && result.failure === null) {
      result.failure = 'attributed_run_limit_exceeded';
    }
  } catch (error) {
    result.failure = text(error);
  }
  return result;
}

function describeAttributedView(view) {
  const attributed = attributedRuns(view);
  if (attributed.text === null) return null;
  const fontRuns = attributed.runs.filter((run) => run.font !== null);
  const fontKeys = Array.from(new Set(fontRuns.map((run) => JSON.stringify(run.font))));
  const paragraphKeys = Array.from(new Set(
    attributed.runs.filter((run) => run.paragraph_style !== null)
      .map((run) => JSON.stringify(run.paragraph_style))));
  const uniqueFont = fontKeys.length === 1 ? JSON.parse(fontKeys[0]) : null;
  const uniqueParagraph = paragraphKeys.length === 1 ? JSON.parse(paragraphKeys[0]) : null;
  return Object.assign({
    native_pointer: text(view.handle),
    native_class: text(view.$className),
    text: attributed.text,
    window_frame: windowFrame(view),
    text_bounds: (() => { try { return rect(view.bounds()); } catch (_) { return null; } })(),
    attributed_runs: attributed.runs,
    attributed_run_status: attributed.failure === null ? 'observed' : 'failed',
    attributed_run_error: attributed.failure,
    font_run_count: fontRuns.length,
    font_run_ambiguity: fontKeys.length > 1,
    paragraph_style_ambiguity: paragraphKeys.length > 1,
    line_fragment_status: 'unsupported',
    line_fragment_bounds: [],
    line_fragment_reason: 'native_line_layout_manager_not_exposed',
  }, uniqueFont || {
    resolved_font_name: null,
    resolved_family_name: null,
    point_size: null,
    symbolic_traits: null,
    effective_weight: null,
  }, uniqueParagraph || {});
}

function describeTextView(view) {
  let font = null;
  try { font = responds(view, 'font') ? view.font() : null; }
  catch (_) { font = null; }
  if (!font) return describeAttributedView(view);
  const traits = descriptorTraits(font);
  return Object.assign({
    native_pointer: text(view.handle),
    native_class: text(view.$className),
    text: viewText(view),
    window_frame: windowFrame(view),
    resolved_font_name: responds(font, 'fontName') ? text(font.fontName()) : null,
    resolved_family_name: responds(font, 'familyName') ? text(font.familyName()) : null,
    point_size: responds(font, 'pointSize') ? number(font.pointSize()) : null,
  }, traits, lineGeometry(view));
}

function walk(root, visitor) {
  let visited = 0;
  const seen = new Set();
  function descend(view, depth) {
    if (!view || depth > MAX_DEPTH || visited >= MAX_VIEWS) return;
    const handle = text(view.handle);
    if (handle !== null && seen.has(handle)) return;
    if (handle !== null) seen.add(handle);
    visited += 1;
    visitor(view);
    for (const child of arrayItems(view.subviews ? view.subviews() : null)) {
      descend(child, depth + 1);
    }
  }
  descend(root, 0);
  return visited;
}

function descendants(root) {
  const result = [];
  let first = true;
  walk(root, (view) => {
    if (first) { first = false; return; }
    result.push(view);
  });
  return result;
}

function windows() {
  const app = ObjC.classes.UIApplication.sharedApplication();
  let values = [];
  try { values = arrayItems(app.windows()); } catch (_) {}
  return values;
}

function resolveStableId(stableId, allWindows) {
  const wrappers = [];
  let visited = 0;
  for (const window of allWindows) {
    visited += walk(window, (view) => {
      if (identifier(view) === stableId) wrappers.push(view);
    });
  }
  const base = {
    stable_id: stableId,
    wrapper_count: wrappers.length,
    visited_view_count: visited,
  };
  if (wrappers.length === 0) {
    return Object.assign(base, {status: 'missing', reason_code: 'stable_id_missing'});
  }
  if (wrappers.length !== 1) {
    return Object.assign(base, {
      status: 'ambiguous', reason_code: 'stable_id_not_unique',
      wrapper_pointers: wrappers.map((view) => text(view.handle)),
    });
  }
  const wrapper = wrappers[0];
  const candidates = [];
  for (const view of [wrapper].concat(descendants(wrapper))) {
    const candidate = describeTextView(view);
    if (candidate !== null) candidates.push(candidate);
  }
  Object.assign(base, {
    wrapper: {
      native_pointer: text(wrapper.handle), native_class: text(wrapper.$className),
      window_frame: windowFrame(wrapper),
    },
    candidate_count: candidates.length,
  });
  if (candidates.length === 0) {
    return Object.assign(base, {
      status: 'unsupported', reason_code: 'native_text_descendant_missing',
    });
  }
  if (candidates.length !== 1) {
    return Object.assign(base, {
      status: 'ambiguous', reason_code: 'native_text_descendant_not_unique',
      candidates: candidates,
    });
  }
  return Object.assign(base, {status: 'observed', reason_code: null}, candidates[0]);
}

function resolveRequest(request, allWindows) {
  const stableId = String(request.stable_id);
  const direct = resolveStableId(stableId, allWindows);
  direct.mapping_method = 'accessibility_identifier';
  direct.mapping_independence = 'native_identifier';
  if (direct.status !== 'missing' || request.text_hint === null ||
      request.text_hint === undefined) {
    return direct;
  }
  const hint = String(request.text_hint);
  const matches = [];
  for (const window of allWindows) {
    walk(window, (view) => {
      if (text(view.$className) !== 'RCTParagraphComponentView') return;
      const candidate = describeAttributedView(view);
      if (candidate !== null && candidate.text === hint) matches.push(candidate);
    });
  }
  const base = {
    stable_id: stableId,
    text_hint: hint,
    mapping_method: 'text_hint_unique',
    mapping_independence: 'diagnostic_same_source',
    candidate_count: matches.length,
  };
  if (matches.length === 0) {
    return Object.assign(base, {status: 'missing', reason_code: 'text_hint_missing'});
  }
  if (matches.length !== 1) {
    return Object.assign(base, {
      status: 'ambiguous', reason_code: 'text_hint_not_unique', candidates: matches,
    });
  }
  return Object.assign(base, {status: 'observed', reason_code: null}, matches[0]);
}

function mappingDiagnostics(allWindows) {
  const identifiers = [];
  const textCandidates = [];
  const nativeClassHistogram = {};
  for (const window of allWindows) {
    walk(window, (view) => {
      const className = text(view.$className) || 'unknown';
      nativeClassHistogram[className] = (nativeClassHistogram[className] || 0) + 1;
      const stableIdentifier = identifier(view);
      if (stableIdentifier !== null && identifiers.length < 300) {
        identifiers.push({
          accessibility_identifier: stableIdentifier,
          native_pointer: text(view.handle),
          native_class: text(view.$className),
          window_frame: windowFrame(view),
        });
      }
      if (textCandidates.length < 300) {
        const candidate = describeTextView(view);
        if (candidate !== null && candidate.text !== null) {
          textCandidates.push(candidate);
        }
      }
    });
  }
  return {
    accessibility_identifier_count: identifiers.length,
    accessibility_identifiers: identifiers,
    native_text_candidate_count: textCandidates.length,
    native_text_candidates: textCandidates,
    native_class_histogram: nativeClassHistogram,
  };
}

function identity() {
  const bundle = ObjC.classes.NSBundle.mainBundle();
  const device = ObjC.classes.UIDevice.currentDevice();
  let contentSizeCategory = null;
  try {
    const allWindows = windows();
    if (allWindows.length > 0) {
      const traits = allWindows[0].traitCollection();
      if (traits && responds(traits, 'preferredContentSizeCategory')) {
        contentSizeCategory = text(traits.preferredContentSizeCategory());
      }
    }
  } catch (_) {}
  return {
    pid: Process.id,
    process_name: Process.name,
    bundle_id: text(bundle.bundleIdentifier()),
    bundle_version: text(bundle.objectForInfoDictionaryKey_('CFBundleShortVersionString')),
    bundle_build: text(bundle.objectForInfoDictionaryKey_('CFBundleVersion')),
    os_name: text(device.systemName()),
    os_version: text(device.systemVersion()),
    preferred_content_size_category: contentSizeCategory,
  };
}

function capture(requests) {
  return new Promise((resolve) => {
    const requestedAt = Date.now();
    ObjC.schedule(ObjC.mainQueue, () => {
      const startedAt = Date.now();
      try {
        const allWindows = windows();
        const elements = requests.map((request) => resolveRequest(request, allWindows));
        resolve({
          ok: true,
          capture_epoch: {
            requested_at_unix_ms: requestedAt,
            main_thread_started_at_unix_ms: startedAt,
            completed_at_unix_ms: Date.now(),
          },
          identity: identity(),
          window_count: allWindows.length,
          elements: elements,
          mapping_diagnostics: mappingDiagnostics(allWindows),
          shadow_tree_diagnostic: null,
        });
      } catch (error) {
        resolve({
          ok: false,
          capture_epoch: {
            requested_at_unix_ms: requestedAt,
            main_thread_started_at_unix_ms: startedAt,
            completed_at_unix_ms: Date.now(),
          },
          identity: identity(),
          failure_code: 'native_capture_failed',
          detail: text(error),
          elements: [],
          shadow_tree_diagnostic: null,
        });
      }
    });
  });
}

rpc.exports = {
  ping() { return 'pong'; },
  capture(stableIds) {
    if (!Array.isArray(stableIds) || stableIds.length === 0) {
      return {
        ok: false, failure_code: 'stable_ids_required', elements: [],
        shadow_tree_diagnostic: null,
      };
    }
    const requests = stableIds.map((value) => {
      if (value && typeof value === 'object' && value.stable_id !== undefined) {
        return {
          stable_id: String(value.stable_id),
          text_hint: value.text_hint === undefined ? null : value.text_hint,
        };
      }
      return {stable_id: String(value), text_hint: null};
    });
    return capture(requests);
  },
};
