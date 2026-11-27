// ax_dump.m — in-process accessibility-tree dumper, injected into a target app.
//
// Walks the host app's own UIView / UIAccessibilityElement properties in-process
// rather than querying the system accessibility server (axserver / VoiceOver),
// so it works reliably in vphone VMs without VoiceOver running.
//
// Build:   scripts/build_ax_dump.sh -> support/ax_dump.dylib (arm64 + arm64e, iphoneos)
// Inject:  loaded into the target app via Frida Module.load (see autophone/device/ax_session.py).
//          On load it activates in-process accessibility, writes /tmp/autophone_ax.loaded,
//          and listens for the com.autophone.ax.dump Darwin notification.
// Trigger: post com.autophone.ax.dump after setting AUTOPHONE_AX_REQUEST_ID.
// Output:  JSON (schema autophone.ax/1) at $AUTOPHONE_AX_PATH or /tmp/autophone_ax.json.
//
// Version history:
// - v6: top-level app activation state (application_state, key_window_scene_activation_state,
//       key_window_scene_class, key_window_is_app_window).
// - v7: node enabled explicitly emitted as JSON boolean.
// - v8: variant_observation (read-only visible text + CoreLocation authorization status).
// - v9: per-node hittability & occlusion annotation ("hittable": BOOL, and when NO
//       "occluded_reason": offscreen_or_empty | hidden_or_transparent | clipped_by_ancestor |
//       modal_sibling | higher_window | hit_test_occluded) via bounds/visibility/clipping,
//       accessibilityViewIsModal sibling suppression, higher UIWindow z-order check, and
//       same-window hitTest:withEvent:. Nodes are annotated, never dropped.
//
// Whenever fields change, bump kDumperVersion and update GUEST_DYLIB + DUMPER_MIN_VERSION
// in autophone/device/ax_session.py.

#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <CoreGraphics/CoreGraphics.h>
#import <CoreLocation/CoreLocation.h>
#import <math.h>
#import <objc/message.h>
#import <dlfcn.h>
#import <notify.h>

static NSString *const kDumpNotification = @"com.autophone.ax.dump";
static NSString *const kLoadedMarker = @"/tmp/autophone_ax.loaded";
// Schema capability revision checked by autophone/device/ax_session.py.
static const NSInteger kDumperVersion = 9;

// Standard UIKit controls (UIButton/UILabel/UITextField) report
// isAccessibilityElement == NO until the UIKit accessibility bundle is loaded.
// Enabling automation via libAccessibility and dlopen'ing the UIKit AX bundle
// activates accessibility in-process without starting VoiceOver. Idempotent.
static void ActivateInProcessAccessibility(void) {
    void *libax = dlopen("/usr/lib/libAccessibility.dylib", RTLD_NOW);
    if (libax) {
        void (*setAutomation)(int) = dlsym(libax, "_AXSSetAutomationEnabled");
        if (setAutomation) setAutomation(1);
    }
    dlopen("/System/Library/AccessibilityBundles/UIKit.axbundle/UIKit", RTLD_NOW);
}

static const NSUInteger kMaxNodes = 4000;   // bound a pathological tree
static const NSUInteger kMaxDepth = 80;
static BOOL dumpTruncated = NO;

static NSString *AXDumpPath(void) {
    const char *env = getenv("AUTOPHONE_AX_PATH");
    return env ? [NSString stringWithUTF8String:env] : @"/tmp/autophone_ax.json";
}

// Decode the UIAccessibilityTraits bitmask into a list of names + a primary role.
static NSArray<NSString *> *DecodeTraits(UIAccessibilityTraits t) {
    NSMutableArray<NSString *> *out = [NSMutableArray array];
    struct { UIAccessibilityTraits bit; const char *name; } map[] = {
        {UIAccessibilityTraitButton,        "button"},
        {UIAccessibilityTraitLink,          "link"},
        {UIAccessibilityTraitHeader,        "header"},
        {UIAccessibilityTraitSearchField,   "searchField"},
        {UIAccessibilityTraitImage,         "image"},
        {UIAccessibilityTraitSelected,      "selected"},
        {UIAccessibilityTraitStaticText,    "staticText"},
        {UIAccessibilityTraitAdjustable,    "adjustable"},
        {UIAccessibilityTraitKeyboardKey,   "keyboardKey"},
        {UIAccessibilityTraitTabBar,        "tabBar"},
        {UIAccessibilityTraitNotEnabled,    "notEnabled"},
    };
    for (size_t i = 0; i < sizeof(map) / sizeof(map[0]); i++) {
        if (t & map[i].bit) [out addObject:[NSString stringWithUTF8String:map[i].name]];
    }
    return out;
}

// Primary role, in priority order, for ax_target's --role filter.
static NSString *PrimaryRole(UIAccessibilityTraits t) {
    if (t & UIAccessibilityTraitButton)      return @"button";
    if (t & UIAccessibilityTraitLink)        return @"link";
    if (t & UIAccessibilityTraitSearchField) return @"searchField";
    if (t & UIAccessibilityTraitHeader)      return @"header";
    if (t & UIAccessibilityTraitTabBar)      return @"tabBar";
    if (t & UIAccessibilityTraitImage)       return @"image";
    if (t & UIAccessibilityTraitAdjustable)  return @"adjustable";
    if (t & UIAccessibilityTraitStaticText)  return @"staticText";
    return @"element";
}

static NSString *SafeStr(id obj) {
    return ([obj isKindOfClass:[NSString class]] && [obj length]) ? obj : @"";
}

static BOOL IsFiniteRect(CGRect r) {
    return !CGRectIsNull(r) && !CGRectIsInfinite(r) &&
           isfinite(r.origin.x) && isfinite(r.origin.y) &&
           isfinite(r.size.width) && isfinite(r.size.height);
}

static NSArray *ChildrenOf(id element) {
    // Prefer the modern container API; fall back to the index-based one, then
    // to UIView subviews. nil means "leaf".
    if ([element respondsToSelector:@selector(accessibilityElements)]) {
        NSArray *kids = [element accessibilityElements];
        if (kids.count) return kids;
    }
    if ([element respondsToSelector:@selector(accessibilityElementCount)]) {
        NSInteger n = [element accessibilityElementCount];
        if (n != NSNotFound && n > 0
            && [element respondsToSelector:@selector(accessibilityElementAtIndex:)]) {
            NSMutableArray *kids = [NSMutableArray array];
            for (NSInteger i = 0; i < n; i++) {
                id k = [element accessibilityElementAtIndex:i];
                if (k) [kids addObject:k];
            }
            if (kids.count) return kids;
        }
    }
    if ([element isKindOfClass:[UIView class]]) {
        return [(UIView *)element subviews];
    }
    return nil;
}

static UIView *ResolveTargetView(id element, UIView *fallbackView) {
    if ([element isKindOfClass:[UIView class]]) {
        return (UIView *)element;
    }
    id cur = element;
    for (NSUInteger hops = 0; cur != nil && hops < 64; hops++) {
        if (![cur respondsToSelector:@selector(accessibilityContainer)]) {
            break;
        }
        id next = [cur accessibilityContainer];
        if (!next || next == cur) {
            break;
        }
        if ([next isKindOfClass:[UIView class]]) {
            return (UIView *)next;
        }
        cur = next;
    }
    return fallbackView;
}

static BOOL IsVisibleModalElement(id child) {
    if (!child) return NO;
    if (![child respondsToSelector:@selector(accessibilityViewIsModal)] ||
        ![child accessibilityViewIsModal]) {
        return NO;
    }
    if ([child isKindOfClass:[UIView class]]) {
        UIView *v = (UIView *)child;
        if (v.hidden || v.alpha <= 0.01) {
            return NO;
        }
    }
    return YES;
}

// Collect all visible interactive UIWindows strictly above `win` in z-order.
// Windows are sorted back-to-front by windowLevel using a stable sort so ties
// preserve original scene/application window array order.
static NSArray<UIWindow *> *HigherWindowsAbove(UIWindow *win, UIScreen *screen) {
    if (!win) return @[];
    NSArray<UIWindow *> *rawWindows = nil;
    if (win.windowScene && win.windowScene.windows.count > 0) {
        rawWindows = win.windowScene.windows;
    } else {
        UIApplication *app = [UIApplication sharedApplication];
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
        rawWindows = app.windows;
#pragma clang diagnostic pop
    }
    if (!rawWindows.count) return @[];

    NSArray<UIWindow *> *sorted = [rawWindows sortedArrayWithOptions:NSSortStable
                                                     usingComparator:^NSComparisonResult(UIWindow *a, UIWindow *b) {
        if (a.windowLevel < b.windowLevel) return NSOrderedAscending;
        if (a.windowLevel > b.windowLevel) return NSOrderedDescending;
        return NSOrderedSame;
    }];

    NSUInteger winIdx = [sorted indexOfObjectIdenticalTo:win];
    NSMutableArray<UIWindow *> *higher = [NSMutableArray array];
    if (winIdx != NSNotFound) {
        for (NSUInteger i = winIdx + 1; i < sorted.count; i++) {
            UIWindow *w = sorted[i];
            if (w.hidden || w.alpha <= 0.01 || !w.userInteractionEnabled) continue;
            if (screen && w.screen && w.screen != screen) continue;
            [higher addObject:w];
        }
    } else {
        for (UIWindow *w in sorted) {
            if (w == win || w.windowLevel <= win.windowLevel) continue;
            if (w.hidden || w.alpha <= 0.01 || !w.userInteractionEnabled) continue;
            if (screen && w.screen && w.screen != screen) continue;
            [higher addObject:w];
        }
    }
    return higher;
}

// Evaluate whether tapping the center of `frame` (screen points) will actually
// reach `element` rather than an occluding modal, backdrop, higher window, or
// clipped/hidden ancestor.
static BOOL EvaluateHittability(id element,
                                CGRect frame,
                                UIView *enclosingView,
                                BOOL modalOccluded,
                                UIWindow *win,
                                UIScreen *screen,
                                CGRect screenBounds,
                                NSArray<UIWindow *> *higherWindows,
                                NSString * __autoreleasing *outReason) {
    // 1. Bounds / empty / offscreen
    if (!IsFiniteRect(frame) || frame.size.width <= 0.0 || frame.size.height <= 0.0) {
        if (outReason) *outReason = @"offscreen_or_empty";
        return NO;
    }
    CGPoint screenCenter = CGPointMake(CGRectGetMidX(frame), CGRectGetMidY(frame));
    if (!isfinite(screenCenter.x) || !isfinite(screenCenter.y) ||
        !CGRectContainsPoint(screenBounds, screenCenter)) {
        if (outReason) *outReason = @"offscreen_or_empty";
        return NO;
    }

    UIView *targetView = ResolveTargetView(element, enclosingView);

    // 1b. Visibility & clipping across targetView -> ancestors
    if (targetView != nil) {
        if (targetView.window == nil) {
            if (outReason) *outReason = @"hidden_or_transparent";
            return NO;
        }
        for (UIView *v = targetView; v != nil; v = v.superview) {
            if (v.hidden || v.alpha <= 0.01) {
                if (outReason) *outReason = @"hidden_or_transparent";
                return NO;
            }
        }
        for (UIView *v = targetView; v != nil; v = v.superview) {
            if (v != element && v.clipsToBounds && v.window != nil) {
                CGRect clipRect = [v convertRect:v.bounds toCoordinateSpace:screen.coordinateSpace];
                if (!IsFiniteRect(clipRect) || CGRectIsEmpty(clipRect) ||
                    !CGRectContainsPoint(clipRect, screenCenter)) {
                    if (outReason) *outReason = @"clipped_by_ancestor";
                    return NO;
                }
                CGPoint ptInV = [v convertPoint:screenCenter fromCoordinateSpace:screen.coordinateSpace];
                if (!isfinite(ptInV.x) || !isfinite(ptInV.y) ||
                    !CGRectContainsPoint(v.bounds, ptInV)) {
                    if (outReason) *outReason = @"clipped_by_ancestor";
                    return NO;
                }
            }
        }
    }

    // 2. accessibilityViewIsModal sibling suppression
    if (modalOccluded) {
        if (outReason) *outReason = @"modal_sibling";
        return NO;
    }

    // 3. Higher UIWindow z-order check
    for (UIWindow *hw in higherWindows) {
        if ([hw respondsToSelector:@selector(accessibilityViewIsModal)] &&
            hw.accessibilityViewIsModal) {
            if (outReason) *outReason = @"higher_window";
            return NO;
        }
        @try {
            CGPoint ptInHW = [hw convertPoint:screenCenter fromCoordinateSpace:screen.coordinateSpace];
            if (isfinite(ptInHW.x) && isfinite(ptInHW.y) &&
                [hw hitTest:ptInHW withEvent:nil] != nil) {
                if (outReason) *outReason = @"higher_window";
                return NO;
            }
        } @catch (NSException *exception) {
            // Do not crash the host app on custom hitTest implementations.
        }
    }

    // 4. Same-window hitTest:withEvent:
    if (!win) {
        if (outReason) *outReason = @"hit_test_occluded";
        return NO;
    }
    UIView *hit = nil;
    @try {
        CGPoint ptInWin = [win convertPoint:screenCenter fromCoordinateSpace:screen.coordinateSpace];
        if (isfinite(ptInWin.x) && isfinite(ptInWin.y)) {
            hit = [win hitTest:ptInWin withEvent:nil];
        }
    } @catch (NSException *exception) {
        hit = nil;
    }
    if (!hit) {
        if (outReason) *outReason = @"hit_test_occluded";
        return NO;
    }
    if (targetView != nil) {
        BOOL related = (hit == targetView) ||
                       [hit isDescendantOfView:targetView] ||
                       [targetView isDescendantOfView:hit];
        if (!related) {
            if (outReason) *outReason = @"hit_test_occluded";
            return NO;
        }
    }
    return YES;
}

static void Walk(id element,
                 NSString *path,
                 NSUInteger depth,
                 UIView *enclosingView,
                 BOOL modalOccluded,
                 UIWindow *win,
                 UIScreen *screen,
                 CGRect screenBounds,
                 NSArray<UIWindow *> *higherWindows,
                 NSMutableArray *nodes) {
    if (element == nil) return;
    if (depth > kMaxDepth || nodes.count >= kMaxNodes) { dumpTruncated = YES; return; }

    BOOL isAE = [element respondsToSelector:@selector(isAccessibilityElement)]
                && [element isAccessibilityElement];
    if (isAE) {
        UIAccessibilityTraits traits = 0;
        if ([element respondsToSelector:@selector(accessibilityTraits)])
            traits = [element accessibilityTraits];
        CGRect f = CGRectZero;
        if ([element respondsToSelector:@selector(accessibilityFrame)])
            f = [element accessibilityFrame];   // screen coords, points

        NSString *ident = @"";
        if ([element respondsToSelector:@selector(accessibilityIdentifier)])
            ident = SafeStr([element accessibilityIdentifier]);

        id value = nil;
        if ([element respondsToSelector:@selector(accessibilityValue)]) {
            id v = [element accessibilityValue];
            value = [v isKindOfClass:[NSString class]] ? v : nil;
        }

        NSString *occludedReason = nil;
        BOOL hittable = EvaluateHittability(element, f, enclosingView, modalOccluded,
                                            win, screen, screenBounds, higherWindows,
                                            &occludedReason);

        CGFloat fx = isfinite(f.origin.x) ? f.origin.x : 0.0;
        CGFloat fy = isfinite(f.origin.y) ? f.origin.y : 0.0;
        CGFloat fw = isfinite(f.size.width) ? f.size.width : 0.0;
        CGFloat fh = isfinite(f.size.height) ? f.size.height : 0.0;

        NSMutableDictionary *node = [NSMutableDictionary dictionary];
        node[@"label"] = SafeStr([element respondsToSelector:@selector(accessibilityLabel)]
                                 ? [element accessibilityLabel] : nil);
        node[@"role"] = PrimaryRole(traits);
        node[@"traits"] = DecodeTraits(traits);
        node[@"frame"] = @[@(fx), @(fy), @(fw), @(fh)];
        node[@"enabled"] = @((BOOL)((traits & UIAccessibilityTraitNotEnabled) == 0));
        node[@"value"] = value ?: [NSNull null];
        node[@"identifier"] = ident;
        node[@"class"] = NSStringFromClass([element class]) ?: @"";
        node[@"path"] = path;
        node[@"hittable"] = @((BOOL)hittable);
        if (!hittable && occludedReason.length) {
            node[@"occluded_reason"] = occludedReason;
        }
        [nodes addObject:node];
        return;   // an accessibility element is a leaf; do not descend
    }

    UIView *nextEnclosing = [element isKindOfClass:[UIView class]] ? (UIView *)element : enclosingView;
    NSArray *kids = ChildrenOf(element);

    // Scan children front-to-back (reverse index order): if a visible child has
    // accessibilityViewIsModal == YES, all earlier (behind) siblings are occluded.
    NSInteger modalCutoff = -1;
    for (NSInteger idx = (NSInteger)kids.count - 1; idx >= 0; idx--) {
        if (IsVisibleModalElement(kids[(NSUInteger)idx])) {
            modalCutoff = idx;
            break;
        }
    }

    NSUInteger i = 0;
    for (id child in kids) {
        if (nodes.count >= kMaxNodes) { dumpTruncated = YES; break; }
        BOOL childModalOccluded = modalOccluded || ((NSInteger)i < modalCutoff);
        Walk(child,
             [NSString stringWithFormat:@"%@/%lu", path, (unsigned long)i],
             depth + 1,
             nextEnclosing,
             childModalOccluded,
             win,
             screen,
             screenBounds,
             higherWindows,
             nodes);
        i++;
    }
}

static UIWindow *KeyWindow(void) {
    UIApplication *app = [UIApplication sharedApplication];
    // iOS 13+: prefer the active scene's key window; fall back to -windows.
    if ([app respondsToSelector:@selector(connectedScenes)]) {
        for (UIScene *scene in app.connectedScenes) {
            if (![scene isKindOfClass:[UIWindowScene class]]) continue;
            UIWindowScene *ws = (UIWindowScene *)scene;
            for (UIWindow *w in ws.windows) {
                if (w.isKeyWindow) return w;
            }
        }
    }
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    for (UIWindow *w in app.windows) {
        if (w.isKeyWindow) return w;
    }
    return app.windows.firstObject;
#pragma clang diagnostic pop
}

// The AX leaves describe visible elements.  This metadata connects them to the
// currently visible app controller without a separate discovery injection.
static UIViewController *VisibleController(UIViewController *vc) {
    while (vc) {
        if (vc.presentedViewController) {
            vc = vc.presentedViewController;
            continue;
        }
        if ([vc isKindOfClass:[UINavigationController class]]) {
            UIViewController *next = [(UINavigationController *)vc visibleViewController];
            if (next) { vc = next; continue; }
        }
        if ([vc isKindOfClass:[UITabBarController class]]) {
            UIViewController *next = [(UITabBarController *)vc selectedViewController];
            if (next) { vc = next; continue; }
        }
        break;
    }
    return vc;
}

// Foreground activation state of the target app (main thread only).
// application_state: UIApplicationState raw value (0 active, 1 inactive, 2 background).
// key_window_scene_*: hosting UIWindowScene of the key window (null when absent).
// key_window_is_app_window: whether key window belongs to a connected scene of this app.
static NSDictionary *ActivationFields(UIWindow *win) {
    UIApplication *app = [UIApplication sharedApplication];
    id sceneState = [NSNull null];
    id sceneClass = [NSNull null];
    BOOL isAppWindow = NO;
    UIWindowScene *scene = win.windowScene;
    if (scene) {
        sceneState = @((NSInteger)scene.activationState);
        sceneClass = NSStringFromClass([scene class]) ?: @"";
        if (win.isKeyWindow && [app.connectedScenes containsObject:scene]) {
            isAppWindow = [scene.windows containsObject:win];
        }
    }
    return @{
        @"application_state": @((NSInteger)app.applicationState),
        @"key_window_scene_activation_state": sceneState,
        @"key_window_scene_class": sceneClass,
        @"key_window_is_app_window": @((BOOL)isAppWindow),
    };
}

// Separate from AX leaf traversal: read-only text evidence for displayed variants.
// Never read secure input, request authorization, or mutate application state.
static void WalkVisibleText(UIView *view, UIWindow *window, NSUInteger depth,
                            NSMutableArray *nodes, NSUInteger *visited, BOOL *complete) {
    if (!view || view.hidden || view.alpha <= 0.01) return;
    if (depth > kMaxDepth || ++(*visited) > kMaxNodes) { *complete = NO; return; }
    if ([view respondsToSelector:@selector(isSecureTextEntry)] &&
        ((BOOL (*)(id, SEL))objc_msgSend)(view, @selector(isSecureTextEntry))) return;
    CGRect f = [view convertRect:view.bounds toView:window];
    if (CGRectIsEmpty(f) || !CGRectIntersectsRect(f, window.bounds)) return;
    NSString *text = nil;
    for (NSString *name in @[@"text", @"attributedText", @"textStorage"]) {
        SEL selector = NSSelectorFromString(name);
        if (![view respondsToSelector:selector]) continue;
        @try {
            id value = ((id (*)(id, SEL))objc_msgSend)(view, selector);
            if ([value isKindOfClass:[NSAttributedString class]]) value = [value string];
            if ([value isKindOfClass:[NSString class]] && [value length]) { text = value; break; }
        } @catch (NSException *exception) { *complete = NO; }
    }
    if (text.length) {
        [nodes addObject:@{@"text": text,
            @"frame": @[@(f.origin.x), @(f.origin.y), @(f.size.width), @(f.size.height)]}];
    }
    for (UIView *child in view.subviews) WalkVisibleText(child, window, depth + 1, nodes, visited, complete);
}

static void WriteDump(void) {
    @autoreleasepool {
        // One-shot per request: only emit when this process received prepare(request_id).
        const char *pending = getenv("AUTOPHONE_AX_REQUEST_ID");
        if (!pending || !*pending) {
            NSLog(@"[autophone-ax] trigger without pending request; skipped");
            return;
        }
        NSString *requestId = [NSString stringWithUTF8String:pending];
        unsetenv("AUTOPHONE_AX_REQUEST_ID");

        // Belt-and-suspenders: ensure AX is active (already called in constructor).
        ActivateInProcessAccessibility();

        UIWindow *win = KeyWindow();
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
        UIScreen *screen = win.screen ?: [UIScreen mainScreen];
#pragma clang diagnostic pop
        CGRect b = screen.bounds;                  // points
        CGFloat scale = screen.scale;              // native = points * scale
        NSArray<UIWindow *> *higherWindows = HigherWindowsAbove(win, screen);

        NSMutableArray *nodes = [NSMutableArray array];
        dumpTruncated = NO;
        Walk(win, @"0", 0, win, NO, win, screen, b, higherWindows, nodes);
        UIViewController *visible = VisibleController(win.rootViewController);

        NSMutableDictionary *dump = [@{
            @"schema": @"autophone.ax/1",
            @"evidence_protocol": @1,
            @"dumper_version": @(kDumperVersion),
            @"request_id": requestId,
            @"complete": @((BOOL)(win != nil && !dumpTruncated)),
            @"truncated": @((BOOL)dumpTruncated),
            @"bundle_version": SafeStr([[NSBundle mainBundle] objectForInfoDictionaryKey:@"CFBundleVersion"]),
            @"short_version": SafeStr([[NSBundle mainBundle] objectForInfoDictionaryKey:@"CFBundleShortVersionString"]),
            @"app": SafeStr([[NSBundle mainBundle] bundleIdentifier]),
            @"captured_at_ms": @((long long)(NSDate.date.timeIntervalSince1970 * 1000.0)),
            @"screen": @{@"scale": @(scale), @"w": @(b.size.width), @"h": @(b.size.height)},
            @"window_class": NSStringFromClass([win class]) ?: @"",
            @"view_controller": visible ? NSStringFromClass([visible class]) : @"",
            @"nodes": nodes,
        } mutableCopy];
        [dump addEntriesFromDictionary:ActivationFields(win)];
        NSMutableArray *textNodes = [NSMutableArray array];
        NSUInteger visited = 0;
        BOOL textComplete = win != nil;
        WalkVisibleText(win, win, 0, textNodes, &visited, &textComplete);
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
        NSInteger locationStatus = (NSInteger)[CLLocationManager authorizationStatus];
#pragma clang diagnostic pop
        dump[@"variant_observation"] = @{
            @"protocol": @1, @"complete": @((BOOL)textComplete),
            @"location_authorization_status": @(locationStatus),
            @"ui_text_nodes": textNodes,
        };

        NSError *err = nil;
        NSData *json = [NSJSONSerialization dataWithJSONObject:dump
                                                      options:NSJSONWritingPrettyPrinted
                                                        error:&err];
        if (!json) {
            NSLog(@"[autophone-ax] JSON encode failed: %@", err);
            return;
        }
        NSString *path = AXDumpPath();
        // Atomic write so the host never file_reads a half-written dump.
        if (![json writeToFile:path atomically:YES]) {
            NSLog(@"[autophone-ax] write failed: %@", path);
            return;
        }
        NSLog(@"[autophone-ax] wrote %lu nodes to %@", (unsigned long)nodes.count, path);
    }
}

static void OnTrigger(CFNotificationCenterRef center, void *observer, CFStringRef name,
                      const void *object, CFDictionaryRef userInfo) {
    // Notification may arrive off the main thread; UIKit AX must run on main.
    dispatch_async(dispatch_get_main_queue(), ^{ WriteDump(); });
}

__attribute__((constructor))
static void AXDumpInit(void) {
    @autoreleasepool {
        // Injection-liveness marker checked on device bring-up.
        [@"ax_dump injected\n" writeToFile:kLoadedMarker atomically:YES
                                  encoding:NSUTF8StringEncoding error:NULL];
        // Activate accessibility at inject time so it settles on the runloop
        // before any dump trigger arrives.
        ActivateInProcessAccessibility();
        CFNotificationCenterAddObserver(
            CFNotificationCenterGetDarwinNotifyCenter(),
            NULL, OnTrigger, (__bridge CFStringRef)kDumpNotification,
            NULL, CFNotificationSuspensionBehaviorCoalesce);
        NSLog(@"[autophone-ax] injected; trigger with: notifyutil -p %@", kDumpNotification);
    }
}
