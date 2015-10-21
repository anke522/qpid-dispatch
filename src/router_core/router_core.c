/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

#include "router_core_private.h"
#include <stdio.h>


ALLOC_DEFINE(qdr_address_t);
ALLOC_DEFINE(qdr_node_t);
ALLOC_DEFINE(qdr_link_ref_t);


qdr_core_t *qdr_core(void)
{
    qdr_core_t *core = NEW(qdr_core_t);
    ZERO(core);

    //
    // Set up the logging source for the router core
    //
    core->log = qd_log_source("ROUTER_CORE");

    //
    // Set up the threading support
    //
    core->cond    = sys_cond();
    core->lock    = sys_mutex();
    core->running = true;
    DEQ_INIT(core->action_list);
    core->thread  = sys_thread(router_core_thread, core);

    return core;
}


void qdr_core_free(qdr_core_t *core)
{
    //
    // Stop and join the thread
    //
    core->running = false;
    sys_cond_signal(core->cond);
    sys_thread_join(core->thread);

    //
    // Free the core resources
    //
    sys_thread_free(core->thread);
    sys_cond_free(core->cond);
    sys_mutex_free(core->lock);
    free(core);
}


ALLOC_DECLARE(qdr_field_t);
ALLOC_DEFINE(qdr_field_t);

qdr_field_t *qdr_field(const char *text)
{
    size_t length  = strlen(text);
    size_t ilength = length;

    if (length == 0)
        return 0;

    qdr_field_t *field   = new_qdr_field_t();
    qd_buffer_t *buf;

    ZERO(field);
    while (length > 0) {
        buf = qd_buffer();
        size_t cap  = qd_buffer_capacity(buf);
        size_t copy = length > cap ? cap : length;
        memcpy(qd_buffer_cursor(buf), text, copy);
        qd_buffer_insert(buf, copy);
        length -= copy;
        text   += copy;
        DEQ_INSERT_TAIL(field->buffers, buf);
    }

    field->iterator = qd_field_iterator_buffer(DEQ_HEAD(field->buffers), 0, ilength);

    return field;
}


void qdr_field_free(qdr_field_t *field)
{
    if (field) {
        qd_field_iterator_free(field->iterator);
        qd_buffer_list_free_buffers(&field->buffers);
        free_qdr_field_t(field);
    }
}


qdr_address_t *qdr_address(qd_address_semantics_t semantics)
{
    qdr_address_t *addr = new_qdr_address_t();
    ZERO(addr);
    addr->semantics = semantics;
    addr->forwarder = qd_router_get_forwarder(semantics);
    return addr;
}


qdr_address_t *qdr_add_local_address(qdr_core_t *core, const char *address, qd_address_semantics_t semantics)
{
    char                 addr_string[1000];
    qdr_address_t       *addr = 0;
    qd_field_iterator_t *iter = 0;

    snprintf(addr_string, sizeof(addr_string), "L%s", address);
    iter = qd_address_iterator_string(addr_string, ITER_VIEW_ALL);

    qd_hash_retrieve(core->addr_hash, iter, (void**) &addr);
    if (!addr) {
        addr = qdr_address(semantics);
        qd_hash_insert(core->addr_hash, iter, addr, &addr->hash_handle);
        DEQ_ITEM_INIT(addr);
        DEQ_INSERT_TAIL(core->addrs, addr);
        addr->block_deletion = true;
    }
    qd_field_iterator_free(iter);
    return addr;
}


void qdr_add_link_ref(qdr_link_ref_list_t *ref_list, qdr_link_t *link)
{
    qdr_link_ref_t *ref = new_qdr_link_ref_t();
    DEQ_ITEM_INIT(ref);
    ref->link = link;
    link->ref = ref;
    DEQ_INSERT_TAIL(*ref_list, ref);
}


void qdr_del_link_ref(qdr_link_ref_list_t *ref_list, qdr_link_t *link)
{
}


void qdr_add_node_ref(qdr_router_ref_list_t *ref_list, qdr_node_t *rnode)
{
}


void qdr_del_node_ref(qdr_router_ref_list_t *ref_list, qdr_node_t *rnode)
{
}

