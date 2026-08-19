#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#include <float.h>


static const NSTimeInterval kConfirmationTimeoutSeconds = 60.0;


@interface ConfirmationTimeout : NSObject
@property(nonatomic, assign) BOOL timedOut;
- (void)fire:(NSTimer *)timer;
@end


@implementation ConfirmationTimeout
- (void)fire:(NSTimer *)timer {
    (void)timer;
    self.timedOut = YES;
    [NSApp abortModal];
}
@end


static NSString *RequiredString(NSDictionary *payload, NSString *key) {
    id value = payload[key];
    return [value isKindOfClass:[NSString class]] ? value : nil;
}


int main(void) {
    @autoreleasepool {
        NSData *input = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
        NSError *jsonError = nil;
        id decoded = [NSJSONSerialization JSONObjectWithData:input options:0 error:&jsonError];
        if (jsonError != nil || ![decoded isKindOfClass:[NSDictionary class]]) {
            fprintf(stderr, "confirmation helper: invalid JSON input\n");
            return 2;
        }

        NSDictionary *payload = decoded;
        NSString *clientName = RequiredString(payload, @"client_name");
        NSString *recipient = RequiredString(payload, @"to");
        NSString *resolvedName = RequiredString(payload, @"resolved_name");
        NSString *service = RequiredString(payload, @"service");
        NSString *message = RequiredString(payload, @"text");
        if (clientName.length == 0 || recipient.length == 0 ||
            service.length == 0 || message.length == 0) {
            fprintf(stderr, "confirmation helper: required field missing\n");
            return 2;
        }

        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];

        NSAlert *alert = [[NSAlert alloc] init];
        alert.alertStyle = NSAlertStyleWarning;
        // Product mode: the wrapper exports the host app's icon path
        // (IMESSAGE_HOST_ICON_PATH). This helper lives outside Contents/MacOS,
        // so without it the alert shows the generic process icon. Baked mode
        // leaves the variable unset and keeps the default.
        const char *iconPath = getenv("IMESSAGE_HOST_ICON_PATH");
        if (iconPath != NULL && iconPath[0] == '/') {
            NSImage *hostIcon = [[NSImage alloc]
                initWithContentsOfFile:[NSString stringWithUTF8String:iconPath]];
            if (hostIcon != nil && hostIcon.isValid) {
                alert.icon = hostIcon;
            }
        }
        alert.messageText = [NSString stringWithFormat:
            @"Confirm %@ %@ Send", clientName, service];
        NSString *displayRecipient = resolvedName.length > 0
            ? [NSString stringWithFormat:@"%@ (%@)", resolvedName, recipient]
            : recipient;
        alert.informativeText = [NSString stringWithFormat:
            @"Recipient: %@\nService: %@\nMessage length: %lu characters",
            displayRecipient,
            service,
            (unsigned long)message.length];

        NSScrollView *scrollView = [[NSScrollView alloc]
            initWithFrame:NSMakeRect(0, 0, 560, 280)];
        scrollView.borderType = NSBezelBorder;
        scrollView.hasVerticalScroller = YES;
        scrollView.hasHorizontalScroller = NO;

        NSTextView *textView = [[NSTextView alloc] initWithFrame:scrollView.bounds];
        textView.editable = NO;
        textView.selectable = YES;
        textView.richText = NO;
        textView.font = [NSFont systemFontOfSize:[NSFont systemFontSize]];
        textView.string = message;
        textView.verticallyResizable = YES;
        textView.horizontallyResizable = NO;
        textView.autoresizingMask = NSViewWidthSizable;
        textView.textContainer.containerSize = NSMakeSize(
            scrollView.contentSize.width, FLT_MAX);
        textView.textContainer.widthTracksTextView = YES;
        scrollView.documentView = textView;
        alert.accessoryView = scrollView;

        NSButton *cancelButton = [alert addButtonWithTitle:@"Cancel"];
        NSButton *sendButton = [alert addButtonWithTitle:@"Send"];
        cancelButton.keyEquivalent = @"\r";
        sendButton.keyEquivalent = @"";

        ConfirmationTimeout *timeout = [[ConfirmationTimeout alloc] init];
        NSTimer *timer = [NSTimer timerWithTimeInterval:kConfirmationTimeoutSeconds
                                                 target:timeout
                                               selector:@selector(fire:)
                                               userInfo:nil
                                                repeats:NO];
        [[NSRunLoop currentRunLoop] addTimer:timer forMode:NSModalPanelRunLoopMode];

        [NSApp activateIgnoringOtherApps:YES];
        NSModalResponse response = [alert runModal];
        [timer invalidate];

        if (timeout.timedOut) {
            return 3;
        }
        return response == NSAlertSecondButtonReturn ? 0 : 1;
    }
}
