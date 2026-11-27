/* Read SpringBoard's VM region metadata without attaching to the process. */
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <mach/mach.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* These libSystem entry points are present on the iOS guest. */
extern int proc_pidpath(int pid, void *buffer, uint32_t size);
extern kern_return_t task_read_for_pid(mach_port_t task, int pid, mach_port_t *result);
extern kern_return_t mach_vm_region_recurse(vm_map_read_t task, mach_vm_address_t *address,
                                           mach_vm_size_t *size, natural_t *depth,
                                           vm_region_recurse_info_t info,
                                           mach_msg_type_number_t *words);

static void protection(vm_prot_t value, char text[4]) {
    text[0] = value & VM_PROT_READ ? 'r' : '-';
    text[1] = value & VM_PROT_WRITE ? 'w' : '-';
    text[2] = value & VM_PROT_EXECUTE ? 'x' : '-';
    text[3] = '\0';
}

static const char *share_mode(unsigned mode) {
    switch (mode) {
        case SM_COW: return "COW";
        case SM_PRIVATE: return "PRV";
        default: return "OTHER";
    }
}

int main(int argc, char **argv) {
    char *end = NULL;
    if (argc != 2) {
        fprintf(stderr, "usage: autophone-springboard-regions PID\n");
        return 2;
    }
    errno = 0;
    long value = strtol(argv[1], &end, 10);
    if (errno || end == argv[1] || *end || value <= 0 || value > INT_MAX) {
        fprintf(stderr, "invalid PID\n");
        return 2;
    }
    int pid = (int)value;
    char path[4096] = {0};
    if (proc_pidpath(pid, path, sizeof(path)) <= 0 ||
        strcmp(path, "/System/Library/CoreServices/SpringBoard.app/SpringBoard") != 0) {
        fprintf(stderr, "PID does not identify SpringBoard\n");
        return 1;
    }

    mach_port_t task = MACH_PORT_NULL;
    kern_return_t kr = task_read_for_pid(mach_task_self(), pid, &task);
    if (kr != KERN_SUCCESS || !MACH_PORT_VALID(task)) {
        fprintf(stderr, "task_read_for_pid failed: %d\n", kr);
        return 1;
    }

    int status = 1;
    unsigned count = 0;
    mach_vm_address_t cursor = 0;
    mach_vm_address_t previous_end = 0;
    natural_t depth = 0;
    for (unsigned query = 0; query < 100000; query++) {
        mach_vm_address_t address = cursor;
        mach_vm_size_t size = 0;
        vm_region_submap_info_data_64_t info = {0};
        mach_msg_type_number_t words = VM_REGION_SUBMAP_INFO_V0_COUNT_64;
        kr = mach_vm_region_recurse(task, &address, &size, &depth,
                                   (vm_region_recurse_info_t)&info, &words);
        if (kr == KERN_INVALID_ADDRESS && count > 0) {
            printf("done %d %u\n", pid, count);
            status = fflush(stdout) == 0 ? 0 : 1;
            break;
        }
        if (kr != KERN_SUCCESS || words != VM_REGION_SUBMAP_INFO_V0_COUNT_64 ||
            address < previous_end || UINT64_MAX - address < size || depth > 64) {
            fprintf(stderr, "region enumeration failed: %d\n", kr);
            break;
        }
        if (size == 0) {
            /* A zero-length record covers no bytes. Query again beyond it.
             * The next returned mapping may begin at the same address. */
            if (address < cursor || address == UINT64_MAX || info.is_submap || info.user_tag == 255) {
                fprintf(stderr, "unexpected empty region record\n");
                break;
            }
            cursor = address + 1;
            continue;
        }
        if (address + size <= cursor) {
            fprintf(stderr, "region enumeration did not advance\n");
            break;
        }
        if (info.is_submap) {
            if (address > cursor) {
                cursor = address;
            }
            depth++;
            continue;
        }
        count++;
        cursor = address + size;
        previous_end = cursor;
        if (info.user_tag == 255) {
            char current[4], maximum[4];
            protection(info.protection, current);
            protection(info.max_protection, maximum);
            printf("tag255 %016" PRIx64 " %016" PRIx64 " %s/%s %s\n",
                   (uint64_t)address, (uint64_t)cursor, current, maximum,
                   share_mode(info.share_mode));
        }
    }
    mach_port_deallocate(mach_task_self(), task);
    return status;
}
