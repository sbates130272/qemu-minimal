/*
 * Timed driver for the P4 fp8 reduction on the emulated rocm-ernic NIC.
 *
 * rocm-ernic's own tests/p4_fp8_client.c loads the P4 program, installs the
 * table entry that turns a two-operand RDMA WRITE into an elementwise fp8 add,
 * and checks one 4096-element reduction. It is a one-shot correctness test.
 * This repeats the same work request and reports a rate.
 *
 * It opens no /dev/ernic-p4 and includes no rocm-ernic header: the control
 * plane and its ABI stay in p4_fp8_client, which must have run first. What is
 * here is plain verbs, and the result of every reduction is checked, so a
 * device with no reduce entry fails instead of timing an ordinary RDMA WRITE.
 *
 * The reduction is in-NIC compute on ONE device. Both operands and the
 * destination are memory regions of the same protection domain, the two queue
 * pairs are connected to each other on the same port, and nothing crosses the
 * mesh to a peer guest.
 *
 * Usage: p4_fp8_bench <ib-device> [elements [iterations]]
 * Prints: p4 fp8 bench: <elements> elements x <iterations> iters,
 *         avg <ms> ms, <rate> MB/s
 * where the rate is fp8 result bytes written per second, one byte an element.
 * The device reads twice that in operands for every byte it writes.
 *
 * SPDX-License-Identifier: MIT
 */

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <infiniband/verbs.h>

/* fp8 E4M3 encodings: 1+2=3 for the even iterations, 4+2=6 for the odd. */
#define FP8_1_0 0x38U
#define FP8_2_0 0x40U
#define FP8_3_0 0x44U
#define FP8_4_0 0x48U
#define FP8_6_0 0x4CU

/* IONIC_FP8_MAP_MAX in rocm-ernic's src/ionic/ionic_datapath_p4.h. */
#define ELEMENTS_MAX 262144UL

#define REFUSED_HINT \
    "  the device refused the operation: one cause is an element count " \
    "above its per-operation cap, which this was built to take as %lu\n"

#define NO_ENTRY_HINT \
    "  no reduce entry is installed on this device: run p4_fp8_client " \
    "first, which loads the P4 program and installs it\n"

static double now_s(void)
{
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static unsigned long parse_count(const char *text, const char *what)
{
    char *end = NULL;
    unsigned long v;

    errno = 0;
    v = strtoul(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || v == 0) {
        fprintf(stderr, "%s must be a positive integer, not '%s'\n", what,
                text);
        exit(2);
    }
    return v;
}

/* Spin rather than sleep: a sleep between polls would be most of the time. */
static int wait_completion(struct ibv_cq *cq, struct ibv_wc *wc)
{
    double deadline = now_s() + 10.0;

    for (;;) {
        int n = ibv_poll_cq(cq, 1, wc);

        if (n < 0)
            return -1;
        if (n > 0)
            return 0;
        if (now_s() > deadline)
            return -1;
    }
}

int main(int argc, char **argv)
{
    unsigned long elements = ELEMENTS_MAX, iters = 200;
    struct ibv_device **dev_list, *ibdev = NULL;
    struct ibv_context *ctx = NULL;
    struct ibv_pd *pd = NULL;
    struct ibv_cq *cq = NULL;
    struct ibv_qp *qp[2] = {NULL, NULL};
    struct ibv_mr *mr_a[2] = {NULL, NULL}, *mr_b = NULL, *mr_c = NULL;
    uint8_t *buf_a[2] = {NULL, NULL}, *buf_b = NULL, *buf_c = NULL;
    const uint8_t fill_a[2] = {FP8_1_0, FP8_4_0};
    const uint8_t want[2] = {FP8_3_0, FP8_6_0};
    struct ibv_qp_init_attr init;
    struct ibv_qp_attr attr;
    union ibv_gid gid;
    long page = sysconf(_SC_PAGESIZE);
    double start, elapsed;
    size_t len;
    int ndev = 0, ret = 1;

    if (argc < 2 || argc > 4) {
        fprintf(stderr, "usage: %s <ib-device> [elements [iterations]]\n",
                argv[0]);
        return 2;
    }
    if (argc > 2)
        elements = parse_count(argv[2], "elements");
    if (argc > 3)
        iters = parse_count(argv[3], "iterations");
    /* Whole pages: the emulated MR mapping rejects anything else. */
    if (elements > ELEMENTS_MAX || elements % (unsigned long)page != 0) {
        fprintf(stderr,
                "elements must be a multiple of %ld and at most %lu\n", page,
                ELEMENTS_MAX);
        return 2;
    }
    len = elements;

    dev_list = ibv_get_device_list(&ndev);
    for (int i = 0; dev_list && i < ndev; i++) {
        if (strcmp(ibv_get_device_name(dev_list[i]), argv[1]) == 0)
            ibdev = dev_list[i];
    }
    if (!ibdev) {
        fprintf(stderr, "no RDMA device named %s\n", argv[1]);
        goto out;
    }
    ctx = ibv_open_device(ibdev);
    if (!ctx) {
        perror("ibv_open_device");
        goto out;
    }
    pd = ibv_alloc_pd(ctx);
    cq = ibv_create_cq(ctx, 16, NULL, NULL, 0);
    if (!pd || !cq) {
        fprintf(stderr, "pd/cq allocation failed\n");
        goto out;
    }

    if (posix_memalign((void **)&buf_a[0], (size_t)page, len) ||
        posix_memalign((void **)&buf_a[1], (size_t)page, len) ||
        posix_memalign((void **)&buf_b, (size_t)page, len) ||
        posix_memalign((void **)&buf_c, (size_t)page, len)) {
        fprintf(stderr, "posix_memalign failed\n");
        goto out;
    }
    memset(buf_a[0], fill_a[0], len);
    memset(buf_a[1], fill_a[1], len);
    memset(buf_b, FP8_2_0, len);
    memset(buf_c, 0, len);

    mr_a[0] = ibv_reg_mr(pd, buf_a[0], len, IBV_ACCESS_LOCAL_WRITE);
    mr_a[1] = ibv_reg_mr(pd, buf_a[1], len, IBV_ACCESS_LOCAL_WRITE);
    mr_b = ibv_reg_mr(pd, buf_b, len, IBV_ACCESS_LOCAL_WRITE);
    mr_c = ibv_reg_mr(pd, buf_c, len,
                      IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE);
    if (!mr_a[0] || !mr_a[1] || !mr_b || !mr_c) {
        perror("ibv_reg_mr");
        goto out;
    }

    memset(&init, 0, sizeof(init));
    init.qp_type = IBV_QPT_RC;
    init.cap.max_send_wr = 8;
    init.cap.max_recv_wr = 8;
    init.cap.max_send_sge = 2;
    init.cap.max_recv_sge = 2;
    init.send_cq = cq;
    init.recv_cq = cq;
    qp[0] = ibv_create_qp(pd, &init);
    qp[1] = ibv_create_qp(pd, &init);
    if (!qp[0] || !qp[1]) {
        perror("ibv_create_qp");
        goto out;
    }

    if (ibv_query_gid(ctx, 1, 0, &gid) != 0) {
        fprintf(stderr, "ibv_query_gid failed\n");
        goto out;
    }
    for (int i = 0; i < 2; i++) {
        memset(&attr, 0, sizeof(attr));
        attr.qp_state = IBV_QPS_INIT;
        attr.port_num = 1;
        attr.qp_access_flags = IBV_ACCESS_REMOTE_READ |
                               IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_LOCAL_WRITE;
        if (ibv_modify_qp(qp[i], &attr,
                          IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT |
                              IBV_QP_ACCESS_FLAGS)) {
            fprintf(stderr, "INIT transition failed\n");
            goto out;
        }
    }
    for (int i = 0; i < 2; i++) {
        memset(&attr, 0, sizeof(attr));
        attr.qp_state = IBV_QPS_RTR;
        attr.path_mtu = IBV_MTU_1024;
        attr.rq_psn = 1;
        attr.max_dest_rd_atomic = 1;
        attr.min_rnr_timer = 12;
        attr.ah_attr.is_global = 1;
        attr.ah_attr.grh.dgid = gid;
        attr.ah_attr.grh.sgid_index = 0;
        attr.ah_attr.grh.hop_limit = 1;
        attr.ah_attr.port_num = 1;
        attr.dest_qp_num = qp[1 - i]->qp_num;
        if (ibv_modify_qp(qp[i], &attr,
                          IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU |
                              IBV_QP_DEST_QPN | IBV_QP_RQ_PSN |
                              IBV_QP_MAX_DEST_RD_ATOMIC |
                              IBV_QP_MIN_RNR_TIMER)) {
            fprintf(stderr, "RTR transition failed\n");
            goto out;
        }
    }
    for (int i = 0; i < 2; i++) {
        memset(&attr, 0, sizeof(attr));
        attr.qp_state = IBV_QPS_RTS;
        attr.timeout = 14;
        attr.retry_cnt = 7;
        attr.rnr_retry = 7;
        attr.sq_psn = 1;
        attr.max_rd_atomic = 1;
        if (ibv_modify_qp(qp[i], &attr,
                          IBV_QP_STATE | IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT |
                              IBV_QP_RNR_RETRY | IBV_QP_SQ_PSN |
                              IBV_QP_MAX_QP_RD_ATOMIC)) {
            fprintf(stderr, "RTS transition failed\n");
            goto out;
        }
    }

    start = now_s();
    for (unsigned long it = 0; it < iters; it++) {
        unsigned int set = (unsigned int)(it & 1U);
        struct ibv_send_wr swr, *bad = NULL;
        struct ibv_sge sge[2];
        struct ibv_wc wc;

        memset(sge, 0, sizeof(sge));
        sge[0].addr = (uint64_t)(uintptr_t)buf_a[set];
        sge[0].length = (uint32_t)len;
        sge[0].lkey = mr_a[set]->lkey;
        sge[1].addr = (uint64_t)(uintptr_t)buf_b;
        sge[1].length = (uint32_t)len;
        sge[1].lkey = mr_b->lkey;

        memset(&swr, 0, sizeof(swr));
        swr.wr_id = it;
        swr.opcode = IBV_WR_RDMA_WRITE;
        swr.send_flags = IBV_SEND_SIGNALED;
        swr.sg_list = sge;
        swr.num_sge = 2;
        swr.wr.rdma.remote_addr = (uint64_t)(uintptr_t)buf_c;
        swr.wr.rdma.rkey = mr_c->rkey;

        if (ibv_post_send(qp[0], &swr, &bad)) {
            perror("ibv_post_send");
            goto out;
        }
        if (wait_completion(cq, &wc) != 0) {
            fprintf(stderr, "no completion for write %lu\n", it);
            goto out;
        }
        if (wc.status != IBV_WC_SUCCESS) {
            fprintf(stderr, "write %lu completed with status %d (%s)\n", it,
                    wc.status, ibv_wc_status_str(wc.status));
            if (wc.status == IBV_WC_REM_ACCESS_ERR)
                fprintf(stderr, NO_ENTRY_HINT);
            else
                fprintf(stderr, REFUSED_HINT, ELEMENTS_MAX);
            goto out;
        }
        /*
         * The operands alternate, so a destination left over from the
         * previous iteration cannot pass. Three bytes in the timed loop,
         * every byte of the last result after it.
         */
        if (buf_c[0] != want[set] || buf_c[len / 2] != want[set] ||
            buf_c[len - 1] != want[set]) {
            fprintf(stderr,
                    "write %lu: destination is %#x, not the fp8 sum %#x\n", it,
                    buf_c[0], want[set]);
            fprintf(stderr, NO_ENTRY_HINT);
            goto out;
        }
    }
    elapsed = now_s() - start;

    for (size_t i = 0; i < len; i++) {
        if (buf_c[i] != want[(iters - 1) & 1U]) {
            fprintf(stderr, "element %zu of the last result is %#x, not %#x\n",
                    i, buf_c[i], want[(iters - 1) & 1U]);
            goto out;
        }
    }

    printf("device=%s\n", ibv_get_device_name(ibdev));
    printf("p4 fp8 bench: %lu elements x %lu iters, avg %.3f ms, %.1f MB/s\n",
           elements, iters, elapsed / (double)iters * 1000.0,
           (double)len * (double)iters / elapsed / 1e6);
    ret = 0;

out:
    for (int i = 0; i < 2; i++) {
        if (qp[i])
            ibv_destroy_qp(qp[i]);
        if (mr_a[i])
            ibv_dereg_mr(mr_a[i]);
    }
    if (mr_b)
        ibv_dereg_mr(mr_b);
    if (mr_c)
        ibv_dereg_mr(mr_c);
    free(buf_a[0]);
    free(buf_a[1]);
    free(buf_b);
    free(buf_c);
    if (cq)
        ibv_destroy_cq(cq);
    if (pd)
        ibv_dealloc_pd(pd);
    if (ctx)
        ibv_close_device(ctx);
    if (dev_list)
        ibv_free_device_list(dev_list);
    return ret;
}
