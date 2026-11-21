#!/usr/bin/env python3
"""A failed surface publication must wake scheduled waiters with its error visible."""
import pathlib
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
events = (root / "plugin/NVMTLEventSync.m").read_text()
objects = (root / "plugin/NVMTLObjects.m").read_text()

def between(text, first, end):
    start = text.index(first)
    return text[start:text.index(end, start)]

finish = between(events, "- (void)nvmtlFinishExecution:", "- (void)nvmtlFinishCallbacks")
early = between(finish, "    if (_scheduleAfterPublish", "    for (NSUInteger i")
status = between(finish, "    _cbError = error;", "    [_completionCondition unlock];")
handlers = between(events, "- (void)nvmtlDispatchHandlers:", "- (void)nvmtlCollectDeviceTiming:")
wait = between(objects, "- (void)waitUntilScheduled {", "- (MTLCommandBufferStatus)status")

fixture = r'''
#import <Foundation/Foundation.h>
#include <assert.h>
enum { MTLCommandBufferStatusCommitted=2, MTLCommandBufferStatusScheduled,
       MTLCommandBufferStatusCompleted, MTLCommandBufferStatusError };
@class NVMTLCommandBuffer;
typedef void (^MTLCommandBufferHandler)(NVMTLCommandBuffer *);
@interface NVMTLCommandQueue : NSObject {
@public dispatch_queue_t _callbackOrderQueue, _completionQueue;
}
@end
@implementation NVMTLCommandQueue @end
@interface NVMTLCommandBuffer : NSObject {
@public
    BOOL _scheduleAfterPublish, _wasScheduled, _executionDone;
    NSUInteger _cbstatus, _nWait;
    NSError *_cbError;
    NSCondition *_completionCondition;
    NSMutableArray *_scheduledHandlers;
    dispatch_group_t _scheduledGroup, _callbackGroup;
    NVMTLCommandQueue *_queue;
}
@end
@implementation NVMTLCommandBuffer
/*HANDLERS*/
/*WAIT*/
- (void)failPublication:(NSError *)error {
/*EARLY*/
    [_completionCondition lock];
/*STATUS*/
    [_completionCondition unlock];
}
@end

int main(void) { @autoreleasepool {
    for (unsigned deferred = 0; deferred < 2; deferred++) {
        NVMTLCommandBuffer *cb = [NVMTLCommandBuffer new];
        cb->_queue = [NVMTLCommandQueue new];
        cb->_completionCondition = [NSCondition new];
        cb->_scheduledGroup = dispatch_group_create();
        cb->_callbackGroup = dispatch_group_create();
        cb->_scheduleAfterPublish = deferred;
        cb->_cbstatus = MTLCommandBufferStatusCommitted;
        NSError *failure = [NSError errorWithDomain:@"test" code:1 userInfo:nil];
        __block unsigned callbacks = 0;
        MTLCommandBufferHandler callback = ^(NVMTLCommandBuffer *buffer) {
            [buffer->_completionCondition lock];
            assert(buffer->_cbstatus == MTLCommandBufferStatusError && buffer->_cbError == failure);
            callbacks++;
            [buffer->_completionCondition unlock];
        };
        cb->_scheduledHandlers = [NSMutableArray arrayWithObject:[callback copy]];
        dispatch_semaphore_t entered = dispatch_semaphore_create(0), done = dispatch_semaphore_create(0);
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            dispatch_semaphore_signal(entered);
            [cb waitUntilScheduled];
            assert(callbacks == 1);
            dispatch_semaphore_signal(done);
        });
        assert(!dispatch_semaphore_wait(entered, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC)));
        [cb failPublication:failure];
        assert(!dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC)));
        assert(!cb->_wasScheduled && cb->_executionDone && !cb->_scheduledHandlers.count);
    }
} }
'''
program = fixture.replace("/*EARLY*/", early).replace("/*STATUS*/", status)
program = program.replace("/*HANDLERS*/", handlers).replace("/*WAIT*/", wait)
with tempfile.TemporaryDirectory() as temp:
    path = pathlib.Path(temp)
    source, binary = path / "failed.m", path / "failed"
    source.write_text(program)
    subprocess.run(["xcrun", "clang", "-fobjc-arc", "-framework", "Foundation",
                    str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=5)
print("PASS: failed submission and deferred publication wake scheduled waiters after their handlers observe Error")
