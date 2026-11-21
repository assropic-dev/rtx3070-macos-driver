#!/usr/bin/env python3
"""Check the production enqueue and submit-ahead paths without loading a GPU driver."""
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
events = (root / "plugin/NVMTLEventSync.m").read_text()
objects = (root / "plugin/NVMTLObjects.m").read_text()


def section(source, first, following):
    start = source.index(first)
    return source[start:source.index(following, start)]


fixture = r"""
#import <Foundation/Foundation.h>
#import <objc/objc-sync.h>
#include <assert.h>
#include <stdatomic.h>

@class NVMTLCommandBuffer;
/* RESERVATION */
@interface TestQueue : NSObject { @public id _reservations; } @end
@implementation TestQueue @end

enum { MTLCommandBufferStatusNotEnqueued, MTLCommandBufferStatusEnqueued };
@interface NVMTLCommandBuffer : NSObject {
@public
    TestQueue *_queue;
    NVMTLQueueReservation *_reservation;
    NSCondition *_completionCondition;
    unsigned _nEnqueue, _cbstatus;
    BOOL ready;
}
- (BOOL)nvmtlSubmitAhead;
- (void)nvmtlReserveQueue;
- (void)nvmtlReleaseReservation;
- (void)enqueue;
@end

// This fixture has only one thread. Releasing and immediately reacquiring one
// level of the recursive class lock tells us who owns it at each collection access.
static void requireClassLock(void) {
    if (objc_sync_exit([NVMTLCommandBuffer class]) != OBJC_SYNC_SUCCESS) {
        fprintf(stderr, "Queue reservations accessed outside the command-buffer class lock\n");
        exit(1);
    }
    assert(objc_sync_enter([NVMTLCommandBuffer class]) == OBJC_SYNC_SUCCESS);
}

@interface CheckedReservations : NSObject <NSCopying> {
    NSMutableArray *entries;
}
@end
@implementation CheckedReservations
- (instancetype)init {
    self = [super init];
    if (self) entries = [NSMutableArray new];
    return self;
}
- (void)addObject:(id)value {
    requireClassLock();
    [entries addObject:value];
}
- (void)removeObjectIdenticalTo:(id)value {
    requireClassLock();
    [entries removeObjectIdenticalTo:value];
}
- (id)copyWithZone:(NSZone *)zone {
    requireClassLock();
    return [entries copyWithZone:zone];
}
@end

static BOOL nvmtl_pumping;
static _Atomic unsigned long long gEvFold;
static NSMutableArray *nvmtl_pending_buffers, *attempts;
@implementation NVMTLCommandBuffer
/* OPERATIONS */
- (BOOL)nvmtlSubmitAhead {
    requireClassLock();
    [attempts addObject:self];
    return ready;
}
@end

/* AHEAD PASS */

int main(void) { @autoreleasepool {
    TestQueue *queue = [TestQueue new];
    queue->_reservations = [CheckedReservations new];
    NVMTLCommandBuffer *first = [NVMTLCommandBuffer new], *second = [NVMTLCommandBuffer new];
    first->_queue = second->_queue = queue;
    [first enqueue];
    [second enqueue];
    second->ready = YES;
    // Reverse commit order to ensure that reservation order determines submission.
    nvmtl_pending_buffers = [NSMutableArray arrayWithObjects:second, first, nil];
    attempts = [NSMutableArray new];
    nvmtl_gsub_ahead_pass();
    assert(attempts.count == 0);
    nvmtl_pumping = YES;
    nvmtl_gsub_ahead_pass();
    assert([attempts isEqual:@[first]]);
    [attempts removeAllObjects];
    first->ready = YES;
    nvmtl_gsub_ahead_pass();
    assert([attempts isEqual:(@[first, second])]);
    @synchronized ([NVMTLCommandBuffer class]) { [first nvmtlReleaseReservation]; }
    [attempts removeAllObjects];
    nvmtl_gsub_ahead_pass();
    assert([attempts isEqual:@[second]]);
} }
"""

reservation = section(events, "@interface NVMTLQueueReservation", "@interface NVMTLSubmissionPart")
operations = section(events, "- (void)nvmtlReserveQueue {", "- (void)nvmtlSealPartWithSignal:")
operations += section(objects, "- (void)enqueue {", "- (void)dealloc {")
ahead = section(events, "static void nvmtl_gsub_ahead_pass1(void);", "- (BOOL)nvmtlAdvancePart")
program = (fixture.replace("/* RESERVATION */", reservation)
           .replace("/* OPERATIONS */", operations)
           .replace("/* AHEAD PASS */", ahead))
with tempfile.TemporaryDirectory() as temp:
    src, exe = Path(temp) / "reservations.m", Path(temp) / "reservations"
    src.write_text(program)
    subprocess.run(["clang", "-fobjc-arc", "-framework", "Foundation", str(src), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
print("PASS: submit-ahead holds the reservation writers' lock and preserves queue order")
