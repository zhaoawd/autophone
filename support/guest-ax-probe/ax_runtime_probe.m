// ax_runtime_probe.m — Standalone read-only out-of-process iOS AXRuntime probe (Stage 3).
//
// Zero third-party injection into target app or SpringBoard; zero UIKit linkage/runloop.
// Minimal verified entitlement on iOS 26.1: com.apple.accessibility.api = true.
//
// HARD SAFETY REDLINE (guest-ax-observation.md §3.3):
// NEVER calls _AXSVoiceOverTouchSetEnabled! Only enables _AXSApplicationAccessibilitySetEnabled(1)
// and _AXSSetAutomationEnabled(1), and verifies _AXSVoiceOverTouchEnabled() == 0.
//
// Usage:
//   guest_ax_probe [--pid <pid>] [--system-wide] [--hit-point <x> <y>]
//                  [--hit-test-nodes] [--no-batch] [--explorer]
//                  [--max-retries <N>] [--retry-delay-ms <ms>] [--request-id <id>]

#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <dlfcn.h>
#import <math.h>
#import <mach/mach_time.h>
#import <objc/message.h>
#import <objc/runtime.h>
#import <unistd.h>

typedef const void *AXUIElementRef;
typedef int32_t AXError;

static const NSUInteger kMaxNodes = 4000;
static const NSInteger kProbeVersion = 1;

// Verified UIAccessibilityTraits bitmasks on iOS 26.1 (extracted from UIKit.framework).
static const uint64_t kTraitButton       = 0x1ULL;
static const uint64_t kTraitLink         = 0x2ULL;
static const uint64_t kTraitImage        = 0x4ULL;
static const uint64_t kTraitSelected     = 0x8ULL;
static const uint64_t kTraitKeyboardKey  = 0x20ULL;
static const uint64_t kTraitStaticText   = 0x40ULL;
static const uint64_t kTraitNotEnabled   = 0x100ULL;
static const uint64_t kTraitSearchField  = 0x400ULL;
static const uint64_t kTraitAdjustable   = 0x1000ULL;
static const uint64_t kTraitTabBar       = 0x8000ULL;
static const uint64_t kTraitHeader       = 0x10000ULL;
static const uint64_t kTraitToggleButton = 0x20000000000000ULL;

static double NowMs(void) {
    static mach_timebase_info_data_t tb = {0, 0};
    if (tb.denom == 0) mach_timebase_info(&tb);
    return (double)mach_absolute_time() * (double)tb.numer / (double)tb.denom / 1e6;
}

static id CallId0(id obj, const char *selName) {
    if (!obj) return nil;
    SEL sel = sel_registerName(selName);
    if (![obj respondsToSelector:sel]) return nil;
    @try { return ((id (*)(id, SEL))objc_msgSend)(obj, sel); }
    @catch (NSException *ex) { return nil; }
}

static int CallInt0(id obj, const char *selName) {
    if (!obj) return 0;
    SEL sel = sel_registerName(selName);
    if (![obj respondsToSelector:sel]) return 0;
    @try { return ((int (*)(id, SEL))objc_msgSend)(obj, sel); }
    @catch (NSException *ex) { return 0; }
}

static BOOL CallBool0(id obj, const char *selName) {
    if (!obj) return NO;
    SEL sel = sel_registerName(selName);
    if (![obj respondsToSelector:sel]) return NO;
    @try { return ((BOOL (*)(id, SEL))objc_msgSend)(obj, sel); }
    @catch (NSException *ex) { return NO; }
}

static uint64_t CallU640(id obj, const char *selName) {
    if (!obj) return 0;
    SEL sel = sel_registerName(selName);
    if (![obj respondsToSelector:sel]) return 0;
    @try { return ((uint64_t (*)(id, SEL))objc_msgSend)(obj, sel); }
    @catch (NSException *ex) { return 0; }
}

static CGRect CallRect0(id obj, const char *selName) {
    if (!obj) return CGRectZero;
    SEL sel = sel_registerName(selName);
    if (![obj respondsToSelector:sel]) return CGRectZero;
    @try { return ((CGRect (*)(id, SEL))objc_msgSend)(obj, sel); }
    @catch (NSException *ex) { return CGRectZero; }
}

static NSString *SafeString(id obj) {
    if (!obj) return @"";
    if ([obj isKindOfClass:[NSString class]]) return (NSString *)obj;
    if ([obj respondsToSelector:@selector(string)]) {
        id s = ((id (*)(id, SEL))objc_msgSend)(obj, @selector(string));
        if ([s isKindOfClass:[NSString class]]) return (NSString *)s;
    }
    if ([obj isKindOfClass:[NSNumber class]]) return [(NSNumber *)obj stringValue];
    return @"";
}

static id SafeNullableString(id obj) {
    NSString *s = SafeString(obj);
    return s.length > 0 ? s : [NSNull null];
}

static NSArray<NSNumber *> *RectArray(CGRect r) {
    double x = isfinite(r.origin.x) ? r.origin.x : 0.0;
    double y = isfinite(r.origin.y) ? r.origin.y : 0.0;
    double w = isfinite(r.size.width) ? r.size.width : 0.0;
    double h = isfinite(r.size.height) ? r.size.height : 0.0;
    return @[@(x), @(y), @(w), @(h)];
}

static NSArray<NSString *> *DecodeTraits(uint64_t t) {
    NSMutableArray<NSString *> *out = [NSMutableArray array];
    struct { uint64_t bit; const char *name; } map[] = {
        {kTraitButton,       "button"},
        {kTraitLink,         "link"},
        {kTraitHeader,       "header"},
        {kTraitSearchField,  "searchField"},
        {kTraitImage,        "image"},
        {kTraitSelected,     "selected"},
        {kTraitStaticText,   "staticText"},
        {kTraitAdjustable,   "adjustable"},
        {kTraitKeyboardKey,  "keyboardKey"},
        {kTraitTabBar,       "tabBar"},
        {kTraitNotEnabled,   "notEnabled"},
        {kTraitToggleButton, "toggleButton"},
    };
    for (size_t i = 0; i < sizeof(map) / sizeof(map[0]); i++) {
        if (t & map[i].bit) [out addObject:@(map[i].name)];
    }
    return out;
}

static NSString *PrimaryRole(uint64_t t) {
    if (t & kTraitButton)      return @"button";
    if (t & kTraitLink)        return @"link";
    if (t & kTraitSearchField) return @"searchField";
    if (t & kTraitHeader)      return @"header";
    if (t & kTraitTabBar)      return @"tabBar";
    if (t & kTraitImage)       return @"image";
    if (t & kTraitAdjustable)  return @"adjustable";
    if (t & kTraitStaticText)  return @"staticText";
    return @"element";
}

static NSArray<NSNumber *> *kBatchAttrs = nil;

static NSDictionary *SerializeNode(id el, BOOL useBatch, BOOL doHitTest, Class AXElementCls) {
    if (!el) return @{};
    id uiEl = CallId0(el, "uiElement");
    if (useBatch && uiEl) {
        SEL enableSel = sel_registerName("enableCache:");
        SEL updateSel = sel_registerName("updateCacheWithAttributes:");
        if ([uiEl respondsToSelector:enableSel] && [uiEl respondsToSelector:updateSel]) {
            ((void (*)(id, SEL, BOOL))objc_msgSend)(uiEl, enableSel, YES);
            ((void (*)(id, SEL, id))objc_msgSend)(uiEl, updateSel, kBatchAttrs);
        }
    }

    NSString *label = SafeString(CallId0(el, "label"));
    NSString *ident = SafeString(CallId0(el, "axIdentifier"));
    NSString *axUid = SafeString(CallId0(el, "identifier"));
    id valueObj = SafeNullableString(CallId0(el, "value"));
    uint64_t traits = CallU640(el, "traits");
    CGRect frame = CallRect0(el, "frame");
    CGRect visFrame = CallRect0(el, "visibleFrame");
    BOOL isVis = CallBool0(el, "isVisible");
    BOOL responds = CallBool0(el, "respondsToUserInteraction");
    BOOL enabled = ((traits & kTraitNotEnabled) == 0);

    NSMutableDictionary *node = [NSMutableDictionary dictionaryWithDictionary:@{
        @"identifier": ident,
        @"ax_uid": axUid,
        @"label": label,
        @"value": valueObj,
        @"role": PrimaryRole(traits),
        @"traits": DecodeTraits(traits),
        @"raw_traits": @(traits),
        @"enabled": @(enabled),
        @"is_visible": @(isVis),
        @"responds_to_user_interaction": @(responds),
        @"frame": RectArray(frame),
        @"visible_frame": RectArray(visFrame),
    }];

    if (doHitTest && AXElementCls && frame.size.width > 0 && frame.size.height > 0) {
        CGPoint center = CGPointMake(CGRectGetMidX(frame), CGRectGetMidY(frame));
        SEL hitSel = sel_registerName("elementAtCoordinate:withVisualPadding:");
        id hit = ((id (*)(id, SEL, CGPoint, BOOL))objc_msgSend)((id)AXElementCls, hitSel, center, NO);
        if (hit) {
            int hitPid = CallInt0(hit, "pid");
            NSString *hitIdent = SafeString(CallId0(hit, "axIdentifier"));
            NSString *hitUid = SafeString(CallId0(hit, "identifier"));
            NSString *hitLabel = SafeString(CallId0(hit, "label"));
            BOOL sameEl = [hit isEqual:el] || (ident.length > 0 && [hitIdent isEqualToString:ident]) ||
                          (axUid.length > 0 && ![axUid isEqualToString:@"0"] && [hitUid isEqualToString:axUid]);
            node[@"hit_test"] = @{
                @"matches_self": @(sameEl),
                @"hit_pid": @(hitPid),
                @"hit_identifier": hitIdent,
                @"hit_label": hitLabel,
            };
        } else {
            node[@"hit_test"] = @{@"matches_self": @NO, @"hit_pid": @0, @"hit_identifier": @"", @"hit_label": @""};
        }
    }

    return node;
}

static NSDictionary *SummarizeApp(id appEl) {
    if (!appEl) return @{};
    return @{
        @"pid": @(CallInt0(appEl, "pid")),
        @"bundle_id": SafeString(CallId0(appEl, "bundleId")),
        @"process_name": SafeString(CallId0(appEl, "processName")),
        @"label": SafeString(CallId0(appEl, "label")),
        @"application_is_modal": @(CallBool0(appEl, "applicationIsModal")),
    };
}

static NSArray<NSDictionary *> *SerializeNodeList(id arr, BOOL useBatch, BOOL doHitTest, Class AXElementCls, BOOL *outTruncated) {
    if (outTruncated) *outTruncated = NO;
    if (![arr isKindOfClass:[NSArray class]]) return @[];
    NSArray *items = (NSArray *)arr;
    NSUInteger limit = items.count;
    if (limit > kMaxNodes) {
        limit = kMaxNodes;
        if (outTruncated) *outTruncated = YES;
    }
    NSMutableArray<NSDictionary *> *nodes = [NSMutableArray arrayWithCapacity:limit];
    for (NSUInteger i = 0; i < limit; i++) {
        [nodes addObject:SerializeNode(items[i], useBatch, doHitTest, AXElementCls)];
    }
    return nodes;
}

static void LookupBundleVersions(NSString *bundleId, NSString **outShort, NSString **outBundle) {
    *outShort = @"";
    *outBundle = @"";
    if (!bundleId || bundleId.length == 0) return;
    @try {
        dlopen("/System/Library/Frameworks/CoreServices.framework/CoreServices", RTLD_LAZY);
        Class lsCls = NSClassFromString(@"LSApplicationProxy");
        if (!lsCls) return;
        SEL sel = sel_registerName("applicationProxyForIdentifier:");
        if (![lsCls respondsToSelector:sel]) return;
        id proxy = ((id (*)(id, SEL, id))objc_msgSend)((id)lsCls, sel, bundleId);
        if (!proxy) return;
        NSString *sv = SafeString(CallId0(proxy, "shortVersionString"));
        NSString *bv = SafeString(CallId0(proxy, "bundleVersion"));
        if (sv.length == 0 || bv.length == 0) {
            id url = CallId0(proxy, "bundleURL");
            NSString *bundlePath = [url isKindOfClass:[NSURL class]] ? [(NSURL *)url path] : SafeString(url);
            if (bundlePath.length > 0) {
                NSString *plistPath = [bundlePath stringByAppendingPathComponent:@"Info.plist"];
                NSDictionary *info = [NSDictionary dictionaryWithContentsOfFile:plistPath];
                if ([info isKindOfClass:[NSDictionary class]]) {
                    if (sv.length == 0) sv = SafeString(info[@"CFBundleShortVersionString"]);
                    if (bv.length == 0) bv = SafeString(info[@"CFBundleVersion"]);
                }
            }
        }
        *outShort = sv ?: @"";
        *outBundle = bv ?: @"";
    } @catch (NSException *ex) {
        *outShort = @"";
        *outBundle = @"";
    }
}

static double DetectDisplayScale(CGRect screenRect) {
    double scale = 0.0;
    @try {
        void *mg = dlopen("/usr/lib/libMobileGestalt.dylib", RTLD_LAZY);
        if (mg) {
            typedef float (*MGFloatFn)(CFStringRef, float);
            typedef CFTypeRef (*MGCopyFn)(CFStringRef, CFDictionaryRef);
            MGFloatFn fnFloat = (MGFloatFn)dlsym(mg, "MGGetFloat32Answer");
            if (fnFloat) {
                float v = fnFloat(CFSTR("main-screen-scale"), 0.0f);
                if (isfinite(v) && v > 0.0f) scale = (double)v;
            }
            if (scale <= 0.0) {
                MGCopyFn fnCopy = (MGCopyFn)dlsym(mg, "MGCopyAnswer");
                if (fnCopy) {
                    CFTypeRef ans = fnCopy(CFSTR("main-screen-scale"), NULL);
                    if (ans) {
                        id obj = (__bridge id)ans;
                        if ([obj isKindOfClass:[NSNumber class]]) {
                            double v = [(NSNumber *)obj doubleValue];
                            if (isfinite(v) && v > 0.0) scale = v;
                        }
                        CFRelease(ans);
                    }
                }
            }
        }
        if (scale <= 0.0) {
            dlopen("/System/Library/Frameworks/QuartzCore.framework/QuartzCore", RTLD_LAZY);
            Class caDisplayCls = NSClassFromString(@"CADisplay");
            id mainDisp = CallId0((id)caDisplayCls, "mainDisplay");
            if (mainDisp) {
                for (NSString *key in @[@"pointScale", @"logicalScale"]) {
                    SEL s = sel_registerName(key.UTF8String);
                    if ([mainDisp respondsToSelector:s]) {
                        double v = ((double (*)(id, SEL))objc_msgSend)(mainDisp, s);
                        if (isfinite(v) && v > 0.0) { scale = v; break; }
                    }
                }
                if (scale <= 0.0 && screenRect.size.width > 0.0) {
                    id mode = CallId0(mainDisp, "currentMode");
                    if (mode) {
                        SEL wSel = sel_registerName("width");
                        if ([mode respondsToSelector:wSel]) {
                            size_t pw = ((size_t (*)(id, SEL))objc_msgSend)(mode, wSel);
                            double ratio = (double)pw / screenRect.size.width;
                            if (isfinite(ratio) && ratio > 0.0) scale = ratio;
                        }
                    }
                }
            }
        }
    } @catch (NSException *ex) {
        scale = 0.0;
    }
    return (isfinite(scale) && scale > 0.0) ? scale : 0.0;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        double tStart = NowMs();

        int targetPid = 0;
        BOOL systemWide = NO;
        BOOL useBatch = YES;
        BOOL useExplorer = NO;
        BOOL hitTestNodes = NO;
        BOOL hasHitPoint = NO;
        double hitX = 0.0, hitY = 0.0;
        int maxRetries = 5;
        int retryDelayMs = 150;
        NSString *requestId = @"";

        for (int i = 1; i < argc; i++) {
            if (strcmp(argv[i], "--pid") == 0 && i + 1 < argc) {
                targetPid = atoi(argv[++i]);
            } else if (strcmp(argv[i], "--system-wide") == 0) {
                systemWide = YES;
            } else if (strcmp(argv[i], "--no-batch") == 0) {
                useBatch = NO;
            } else if (strcmp(argv[i], "--explorer") == 0) {
                useExplorer = YES;
            } else if (strcmp(argv[i], "--hit-test-nodes") == 0) {
                hitTestNodes = YES;
            } else if (strcmp(argv[i], "--hit-point") == 0 && i + 2 < argc) {
                hasHitPoint = YES;
                hitX = atof(argv[++i]);
                hitY = atof(argv[++i]);
            } else if (strcmp(argv[i], "--max-retries") == 0 && i + 1 < argc) {
                maxRetries = atoi(argv[++i]);
                if (maxRetries < 0) maxRetries = 0;
            } else if (strcmp(argv[i], "--retry-delay-ms") == 0 && i + 1 < argc) {
                retryDelayMs = atoi(argv[++i]);
                if (retryDelayMs < 10) retryDelayMs = 10;
                if (retryDelayMs > 2000) retryDelayMs = 2000;
            } else if (strcmp(argv[i], "--request-id") == 0 && i + 1 < argc) {
                requestId = [NSString stringWithUTF8String:argv[++i]] ?: @"";
            }
        }

        // Only batch-prefetch the 3 attributes NOT already prefetched by visibleElements:
        // 2025=isVisible, 5019=axIdentifier, 12000=respondsToUserInteraction.
        // (2001=label, 2003=frame, 2004=traits, 2006=value, 2031=ax_uid, 2057=visibleFrame
        //  are already populated in AXUIElement's cache by visibleElements!)
        kBatchAttrs = @[@2025, @5019, @12000];

        // 1. Load libAccessibility & enable Application Accessibility + Automation (NEVER VoiceOver!)
        void *libax = dlopen("/usr/lib/libAccessibility.dylib", RTLD_LAZY);
        typedef unsigned char (*BoolGetterFn)(void);
        typedef void (*BoolSetterFn)(unsigned char);
        BoolGetterFn getAppAx = libax ? (BoolGetterFn)dlsym(libax, "_AXSApplicationAccessibilityEnabled") : NULL;
        BoolSetterFn setAppAx = libax ? (BoolSetterFn)dlsym(libax, "_AXSApplicationAccessibilitySetEnabled") : NULL;
        BoolGetterFn getAuto  = libax ? (BoolGetterFn)dlsym(libax, "_AXSAutomationEnabled") : NULL;
        BoolSetterFn setAuto  = libax ? (BoolSetterFn)dlsym(libax, "_AXSSetAutomationEnabled") : NULL;
        BoolGetterFn getVO    = libax ? (BoolGetterFn)dlsym(libax, "_AXSVoiceOverTouchEnabled") : NULL;

        BOOL didEnable = NO;
        if (getAppAx && setAppAx && !getAppAx()) {
            setAppAx(1);
            didEnable = YES;
        }
        if (getAuto && setAuto && !getAuto()) {
            setAuto(1);
            didEnable = YES;
        }

        // 2. Load AXRuntime.framework
        void *axrt = dlopen("/System/Library/PrivateFrameworks/AXRuntime.framework/AXRuntime", RTLD_LAZY);
        if (!axrt) {
            fprintf(stderr, "ax_runtime_probe: failed to load AXRuntime: %s\n", dlerror() ?: "unknown");
            return 2;
        }

        typedef AXUIElementRef (*CreateSysWideFn)(void);
        typedef AXUIElementRef (*CreateAppPidFn)(pid_t);
        typedef AXError (*SetTimeoutFn)(AXUIElementRef, float);
        CreateSysWideFn fnSysWide = (CreateSysWideFn)dlsym(axrt, "AXUIElementCreateSystemWide");
        CreateAppPidFn fnAppPid = (CreateAppPidFn)dlsym(axrt, "_AXUIElementCreateAppElementWithPid");
        SetTimeoutFn fnSetTimeout = (SetTimeoutFn)dlsym(axrt, "AXUIElementSetMessagingTimeout");

        if (fnSysWide && fnSetTimeout) {
            AXUIElementRef sw = fnSysWide();
            if (sw) { fnSetTimeout(sw, 2.0f); CFRelease(sw); }
        }

        Class AXElementCls = NSClassFromString(@"AXElement");
        if (!AXElementCls) {
            fprintf(stderr, "ax_runtime_probe: AXElement class unavailable\n");
            return 2;
        }

        // 3. Bounded first-wake retry loop (up to maxRetries x 150ms when root/app tree is empty)
        id sysApp = nil;
        id targetApp = nil;
        NSArray *visElements = nil;
        int retries = 0;

        for (int attempt = 0; attempt <= maxRetries; attempt++) {
            if (attempt > 0 || didEnable) {
                if (attempt > 0) {
                    retries = attempt;
                    usleep((useconds_t)(retryDelayMs * 1000));
                } else if (didEnable) {
                    usleep((useconds_t)(retryDelayMs * 1000));
                }
            }
            sysApp = CallId0((id)AXElementCls, "systemApplication");
            int sysPidNow = sysApp ? CallInt0(sysApp, "pid") : 0;
            BOOL sysLockedNow = sysApp ? CallBool0(sysApp, "isScreenLocked") : NO;
            BOOL sysCCNow = sysApp ? CallBool0(sysApp, "isControlCenterVisible") : NO;
            id primNow = CallId0((id)AXElementCls, "primaryApp");
            int primPidNow = primNow ? CallInt0(primNow, "pid") : 0;

            // Never issue AX Mach IPC to a backgrounded/iOS-suspended targetPid (can block
            // until shell timeout when the target app is suspended on Home screen).
            if (targetPid > 0 && targetPid != sysPidNow) {
                if (sysLockedNow || sysCCNow || (primPidNow > 0 && primPidNow != targetPid)) {
                    targetApp = nil;
                    visElements = nil;
                    break;
                }
                if (primPidNow == 0) {
                    targetApp = nil;
                    continue;
                }
            }

            if (targetPid > 0 && fnAppPid) {
                AXUIElementRef ref = fnAppPid(targetPid);
                if (ref) {
                    if (fnSetTimeout) fnSetTimeout(ref, 2.0f);
                    SEL elWithRef = sel_registerName("elementWithAXUIElement:");
                    targetApp = ((id (*)(id, SEL, AXUIElementRef))objc_msgSend)((id)AXElementCls, elWithRef, ref);
                    CFRelease(ref);
                }
            } else {
                targetApp = primNow ?: CallId0((id)AXElementCls, "primaryApp");
                if (!targetApp && sysApp) {
                    targetApp = CallId0(sysApp, "currentApplication");
                }
            }

            if (targetApp) {
                id v = CallId0(targetApp, useExplorer ? "explorerElements" : "visibleElements");
                if ([v isKindOfClass:[NSArray class]] && [(NSArray *)v count] > 0) {
                    visElements = (NSArray *)v;
                    break;
                }
            }
            // If SpringBoard Control Center / Lock Screen is foreground, targetApp's tree may be
            // occluded or empty; break early to avoid stalling the retry loop.
            if (sysLockedNow || sysCCNow) {
                break;
            }
        }

        // 4. Collect system-level overlay / occlusion context from SpringBoard
        CGRect screenRect = sysApp ? CallRect0(sysApp, "frame") : (targetApp ? CallRect0(targetApp, "frame") : CGRectZero);
        double displayScale = DetectDisplayScale(screenRect);
        int resolvedPid = targetApp ? CallInt0(targetApp, "pid") : targetPid;
        int sysPid = sysApp ? CallInt0(sysApp, "pid") : 0;
        NSString *resolvedBundleId = targetApp ? SafeString(CallId0(targetApp, "bundleId")) : @"";
        NSString *shortVersion = @"";
        NSString *bundleVersion = @"";
        LookupBundleVersions(resolvedBundleId, &shortVersion, &bundleVersion);

        NSMutableDictionary *sysDict = [NSMutableDictionary dictionary];
        NSArray *sbVis = nil;
        BOOL sbTruncated = NO;
        if (sysApp) {
            sysDict[@"is_screen_locked"] = @(CallBool0(sysApp, "isScreenLocked"));
            sysDict[@"is_control_center_visible"] = @(CallBool0(sysApp, "isControlCenterVisible"));
            sysDict[@"focused_scene_identifier"] = SafeString(CallId0(sysApp, "focusedSceneIdentifier"));
            id occ = CallId0(sysApp, "focusedOccludedAppSceneIdentifiers");
            sysDict[@"focused_occluded_app_scene_identifiers"] = [occ isKindOfClass:[NSArray class]] ? occ : @[];
            id prim = CallId0((id)AXElementCls, "primaryApp");
            sysDict[@"primary_app"] = prim ? SummarizeApp(prim) : [NSNull null];
            id curApps = CallId0(sysApp, "currentApplications");
            NSMutableArray *appList = [NSMutableArray array];
            if ([curApps isKindOfClass:[NSArray class]]) {
                for (id a in (NSArray *)curApps) [appList addObject:SummarizeApp(a)];
            }
            sysDict[@"current_applications"] = appList;
            if (systemWide) {
                if (sysPid > 0 && sysPid == resolvedPid) {
                    sysDict[@"springboard_visible_count"] = @([visElements isKindOfClass:[NSArray class]] ? [(NSArray *)visElements count] : 0);
                } else {
                    sbVis = CallId0(sysApp, "visibleElements");
                    sysDict[@"springboard_visible_count"] = [sbVis isKindOfClass:[NSArray class]] ? @([sbVis count]) : @0;
                }
            }
        }

        // 5. Serialize target app nodes (and SpringBoard nodes when --system-wide)
        BOOL nodesTruncated = NO;
        NSArray<NSDictionary *> *nodes = SerializeNodeList(visElements, useBatch, hitTestNodes, AXElementCls, &nodesTruncated);
        NSArray<NSDictionary *> *sbNodes = (systemWide && sbVis) ? SerializeNodeList(sbVis, useBatch, NO, nil, &sbTruncated) : nil;
        CGRect appFrame = targetApp ? CallRect0(targetApp, "frame") : CGRectZero;

        // 6. Optional point hit-test across all processes via backboardd/AXRuntime
        NSDictionary *hitPointResult = nil;
        if (hasHitPoint) {
            CGPoint pt = CGPointMake(hitX, hitY);
            SEL hitSel = sel_registerName("elementAtCoordinate:withVisualPadding:");
            SEL appAtSel = sel_registerName("applicationAtCoordinate:");
            id hitEl = ((id (*)(id, SEL, CGPoint, BOOL))objc_msgSend)((id)AXElementCls, hitSel, pt, NO);
            id hitApp = ((id (*)(id, SEL, CGPoint))objc_msgSend)((id)AXElementCls, appAtSel, pt);
            NSMutableDictionary *h = [NSMutableDictionary dictionary];
            h[@"point"] = @[@(hitX), @(hitY)];
            h[@"app"] = hitApp ? SummarizeApp(hitApp) : [NSNull null];
            if (hitEl) {
                NSMutableDictionary *elInfo = [SerializeNode(hitEl, useBatch, NO, nil) mutableCopy];
                elInfo[@"pid"] = @(CallInt0(hitEl, "pid"));
                elInfo[@"bundle_id"] = SafeString(CallId0(hitEl, "bundleId"));
                elInfo[@"process_name"] = SafeString(CallId0(hitEl, "processName"));
                h[@"element"] = elInfo;
            } else {
                h[@"element"] = [NSNull null];
            }
            hitPointResult = h;
        }

        double elapsedMs = NowMs() - tStart;
        long long capturedAtMs = (long long)(NSDate.date.timeIntervalSince1970 * 1000.0);

        NSMutableDictionary *out = [NSMutableDictionary dictionaryWithDictionary:@{
            @"schema": @"autophone.ax_probe/1",
            @"probe_version": @(kProbeVersion),
            @"ax_backend": @"guest_axruntime",
            @"request_id": requestId ?: @"",
            @"captured_at_ms": @(capturedAtMs),
            @"complete": (!nodesTruncated ? @YES : @NO),
            @"truncated": (nodesTruncated ? @YES : @NO),
            @"pid": @(resolvedPid),
            @"bundle_id": resolvedBundleId,
            @"short_version": shortVersion,
            @"bundle_version": bundleVersion,
            @"process_name": targetApp ? SafeString(CallId0(targetApp, "processName")) : @"",
            @"elapsed_ms": @(round(elapsedMs * 100.0) / 100.0),
            @"first_wake_retries": @(retries),
            @"did_enable_ax": (didEnable ? @YES : @NO),
            @"batch_mode": (useBatch ? @YES : @NO),
            @"ax_state": @{
                @"app_accessibility": getAppAx ? ((getAppAx() != 0) ? @YES : @NO) : [NSNull null],
                @"automation": getAuto ? ((getAuto() != 0) ? @YES : @NO) : [NSNull null],
                @"voiceover": getVO ? ((getVO() != 0) ? @YES : @NO) : [NSNull null],
            },
            @"screen": @{
                @"x": @(isfinite(screenRect.origin.x) ? screenRect.origin.x : 0.0),
                @"y": @(isfinite(screenRect.origin.y) ? screenRect.origin.y : 0.0),
                @"w": @(isfinite(screenRect.size.width) ? screenRect.size.width : 0.0),
                @"h": @(isfinite(screenRect.size.height) ? screenRect.size.height : 0.0),
                @"scale": @(displayScale),
                @"app_frame": RectArray(appFrame),
            },
            @"system": sysDict,
            @"node_count": @(nodes.count),
            @"nodes": nodes,
        }];
        if (sbNodes) out[@"springboard_nodes"] = sbNodes;
        if (hitPointResult) out[@"hit_test"] = hitPointResult;

        NSError *jerr = nil;
        NSData *json = [NSJSONSerialization dataWithJSONObject:out options:0 error:&jerr];
        if (!json) {
            fprintf(stderr, "ax_runtime_probe: JSON serialization failed: %s\n",
                    jerr.localizedDescription.UTF8String);
            return 3;
        }
        fwrite(json.bytes, 1, json.length, stdout);
        fputc('\n', stdout);
    }
    return 0;
}
