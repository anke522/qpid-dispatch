#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import
from __future__ import print_function

import unittest2 as unittest
from time import sleep, time
from threading import Thread, Event
from subprocess import PIPE, STDOUT

from system_test import TestCase, Qdrouterd, main_module, TIMEOUT, Process

from proton import Message
from proton.handlers import MessagingHandler
from proton.reactor import AtMostOnce, Container, DynamicNodeProperties, LinkOption, AtLeastOnce
from proton.utils import BlockingConnection
from system_tests_drain_support import DrainMessagesHandler, DrainOneMessageHandler, DrainNoMessagesHandler, DrainNoMoreMessagesHandler

from qpid_dispatch.management.client import Node
from qpid_dispatch.management.error import NotFoundStatus


class LinkRouteTest(TestCase):
    """
    Tests the linkRoute property of the dispatch router.

    Sets up 4 routers (two of which are acting as brokers (QDR.A, QDR.D)). The other two routers have linkRoutes
    configured such that matching traffic will be directed to/from the 'fake' brokers.

    (please see configs in the setUpClass method to get a sense of how the routers and their connections are configured)
    The tests in this class send and receive messages across this network of routers to link routable addresses.
    Uses the Python Blocking API to send/receive messages. The blocking api plays neatly into the synchronous nature
    of system tests.

        QDR.A acting broker #1
             +---------+         +---------+         +---------+     +-----------------+
             |         | <------ |         | <-----  |         |<----| blocking_sender |
             |  QDR.A  |         |  QDR.B  |         |  QDR.C  |     +-----------------+
             |         | ------> |         | ------> |         |     +-------------------+
             +---------+         +---------+         +---------+---->| blocking_receiver |
                                    ^  |                             +-------------------+
                                    |  |
                                    |  V
                                 +---------+
                                 |         |
                                 |  QDR.D  |
                                 |         |
                                 +---------+
                            QDR.D acting broker #2

    """
    @classmethod
    def get_router(cls, index):
        return cls.routers[index]

    @classmethod
    def setUpClass(cls):
        """Start three routers"""
        super(LinkRouteTest, cls).setUpClass()

        def router(name, connection):

            config = [
                ('router', {'mode': 'interior', 'id': 'QDR.%s'%name}),
            ] + connection

            config = Qdrouterd.Config(config)
            cls.routers.append(cls.tester.qdrouterd(name, config, wait=False))

        cls.routers = []
        a_listener_port = cls.tester.get_port()
        b_listener_port = cls.tester.get_port()
        c_listener_port = cls.tester.get_port()
        d_listener_port = cls.tester.get_port()
        test_tag_listener_port = cls.tester.get_port()

        router('A',
               [
                   ('listener', {'role': 'normal', 'host': '0.0.0.0', 'port': a_listener_port, 'saslMechanisms': 'ANONYMOUS'}),
               ])
        router('B',
               [
                   # Listener for clients, note that the tests assume this listener is first in this list:
                   ('listener', {'role': 'normal', 'host': '0.0.0.0', 'port': b_listener_port, 'saslMechanisms': 'ANONYMOUS'}),
                   ('listener', {'name': 'test-tag', 'role': 'route-container', 'host': '0.0.0.0', 'port': test_tag_listener_port, 'saslMechanisms': 'ANONYMOUS'}),

                   # This is an route-container connection made from QDR.B's ephemeral port to a_listener_port
                   ('connector', {'name': 'broker', 'role': 'route-container', 'host': '0.0.0.0', 'port': a_listener_port, 'saslMechanisms': 'ANONYMOUS'}),
                   # Only inter router communication must happen on 'inter-router' connectors. This connector makes
                   # a connection from the router B's ephemeral port to c_listener_port
                   ('connector', {'name': 'routerC', 'role': 'inter-router', 'host': '0.0.0.0', 'port': c_listener_port}),
                   # This is an on-demand connection made from QDR.B's ephemeral port to d_listener_port
                   ('connector', {'name': 'routerD', 'role': 'route-container', 'host': '0.0.0.0', 'port': d_listener_port, 'saslMechanisms': 'ANONYMOUS'}),

                   #('linkRoute', {'prefix': 'org.apache', 'connection': 'broker', 'direction': 'in'}),
                   ('linkRoute', {'prefix': 'org.apache', 'containerId': 'QDR.A', 'direction': 'in'}),
                   ('linkRoute', {'prefix': 'org.apache', 'containerId': 'QDR.A', 'direction': 'out'}),

                   ('linkRoute', {'prefix': 'pulp.task', 'connection': 'test-tag', 'direction': 'in'}),
                   ('linkRoute', {'prefix': 'pulp.task', 'connection': 'test-tag', 'direction': 'out'}),

                   # addresses matching pattern 'a.*.toA.#' route to QDR.A
                   ('linkRoute', {'pattern': 'a.*.toA.#', 'containerId': 'QDR.A', 'direction': 'in'}),
                   ('linkRoute', {'pattern': 'a.*.toA.#', 'containerId': 'QDR.A', 'direction': 'out'}),

                   # addresses matching pattern 'a.*.toD.#' route to QDR.D
                   # Dont change dir to direction here so we can make sure that the dir attribute is still working.
                   ('linkRoute', {'pattern': 'a.*.toD.#', 'containerId': 'QDR.D', 'dir': 'in'}),
                   ('linkRoute', {'pattern': 'a.*.toD.#', 'containerId': 'QDR.D', 'dir': 'out'})

               ]
               )
        router('C',
               [
                   # The client will exclusively use the following listener to
                   # connect to QDR.C, the tests assume this is the first entry
                   # in the list
                   ('listener', {'host': '0.0.0.0', 'role': 'normal', 'port': cls.tester.get_port(), 'saslMechanisms': 'ANONYMOUS'}),
                   ('listener', {'host': '0.0.0.0', 'role': 'inter-router', 'port': c_listener_port, 'saslMechanisms': 'ANONYMOUS'}),
                   # The dot(.) at the end is ignored by the address hashing scheme.

                   ('linkRoute', {'prefix': 'org.apache.', 'direction': 'in'}),
                   ('linkRoute', {'prefix': 'org.apache.', 'direction': 'out'}),

                   ('linkRoute', {'prefix': 'pulp.task', 'direction': 'in'}),
                   ('linkRoute', {'prefix': 'pulp.task', 'direction': 'out'}),

                   ('linkRoute', {'pattern': 'a.*.toA.#', 'direction': 'in'}),
                   ('linkRoute', {'pattern': 'a.*.toA.#', 'direction': 'out'}),

                   ('linkRoute', {'pattern': 'a.*.toD.#', 'direction': 'in'}),
                   ('linkRoute', {'pattern': 'a.*.toD.#', 'direction': 'out'})

                ]
               )
        router('D',  # sink for QDR.D routes
               [
                   ('listener', {'role': 'normal', 'host': '0.0.0.0', 'port': d_listener_port, 'saslMechanisms': 'ANONYMOUS'}),
               ])

        # Wait for the routers to locate each other, and for route propagation
        # to settle
        cls.routers[1].wait_router_connected('QDR.C')
        cls.routers[2].wait_router_connected('QDR.B')
        cls.routers[2].wait_address("org.apache", remotes=1, delay=0.5)

        # This is not a classic router network in the sense that QDR.A and D are acting as brokers. We allow a little
        # bit more time for the routers to stabilize.
        sleep(2)

    def run_qdstat_linkRoute(self, address, args=None):
        cmd = ['qdstat', '--bus', str(address), '--timeout', str(TIMEOUT) ] + ['--linkroute']
        if args:
            cmd = cmd + args
        p = self.popen(
            cmd,
            name='qdstat-'+self.id(), stdout=PIPE, expect=None,
            universal_newlines=True)

        out = p.communicate()[0]
        assert p.returncode == 0, "qdstat exit status %s, output:\n%s" % (p.returncode, out)
        return out

    def run_qdmanage(self, cmd, input=None, expect=Process.EXIT_OK, address=None):
        p = self.popen(
            ['qdmanage'] + cmd.split(' ') + ['--bus', address or self.address(), '--indent=-1', '--timeout', str(TIMEOUT)],
            stdin=PIPE, stdout=PIPE, stderr=STDOUT, expect=expect,
            universal_newlines=True)
        out = p.communicate(input)[0]
        try:
            p.teardown()
        except Exception as e:
            raise Exception("%s\n%s" % (e, out))
        return out

    def test_aaa_qdmanage_query_link_route(self):
        """
        qdmanage converts short type to long type and this test specifically tests if qdmanage is actually doing
        the type conversion correctly by querying with short type and long type.
        """
        cmd = 'QUERY --type=linkRoute'
        out = self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])

        # Make sure there is a dir of in and out.
        self.assertTrue('"direction": "in"' in out)
        self.assertTrue('"direction": "out"' in out)
        self.assertTrue('"containerId": "QDR.A"' in out)

        # Use the long type and make sure that qdmanage does not mess up the long type
        cmd = 'QUERY --type=org.apache.qpid.dispatch.router.config.linkRoute'
        out = self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])

        # Make sure there is a dir of in and out.
        self.assertTrue('"direction": "in"' in out)
        self.assertTrue('"direction": "out"' in out)
        self.assertTrue('"containerId": "QDR.A"' in out)

        identity = out[out.find("identity") + 12: out.find("identity") + 13]
        cmd = 'READ --type=linkRoute --identity=' + identity
        out = self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])
        self.assertTrue(identity in out)

        exception_occurred = False
        try:
            # This identity should not be found
            cmd = 'READ --type=linkRoute --identity=9999'
            out = self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])
        except Exception as e:
            exception_occurred = True
            self.assertTrue("NotFoundStatus: Not Found" in str(e))

        self.assertTrue(exception_occurred)

        exception_occurred = False
        try:
            # There is no identity specified, this is a bad request
            cmd = 'READ --type=linkRoute'
            out = self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])
        except Exception as e:
            exception_occurred = True
            self.assertTrue("BadRequestStatus: No name or identity provided" in str(e))

        self.assertTrue(exception_occurred)

        cmd = 'CREATE --type=autoLink addr=127.0.0.1 direction=in connection=routerC'
        out = self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])

        identity = out[out.find("identity") + 12: out.find("identity") + 14]
        cmd = 'READ --type=autoLink --identity=' + identity
        out = self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])
        self.assertTrue(identity in out)

    def test_bbb_qdstat_link_routes_routerB(self):
        """
        Runs qdstat on router B to make sure that router B has 4 link routes,
        each having one 'in' and one 'out' entry

        """
        out = self.run_qdstat_linkRoute(self.routers[1].addresses[0])
        for route in ['a.*.toA.#', 'a.*.toD.#', 'org.apache',  'pulp.task']:
            self.assertTrue(route in out)

        out_list = out.split()
        self.assertEqual(out_list.count('in'), 4)
        self.assertEqual(out_list.count('out'), 4)

        parts = out.split("\n")
        self.assertEqual(len(parts), 12)

        out = self.run_qdstat_linkRoute(self.routers[1].addresses[0], args=['--limit=1'])
        parts = out.split("\n")
        self.assertEqual(len(parts), 5)

    def test_ccc_qdstat_link_routes_routerC(self):
        """
        Runs qdstat on router C to make sure that router C has 4 link routes,
        each having one 'in' and one 'out' entry

        """
        out = self.run_qdstat_linkRoute(self.routers[2].addresses[0])
        out_list = out.split()

        self.assertEqual(out_list.count('in'), 4)
        self.assertEqual(out_list.count('out'), 4)

    def test_ddd_partial_link_route_match(self):
        """
        The linkRoute on Routers C and B is set to org.apache.
        Creates a receiver listening on the address 'org.apache.dev' and a sender that sends to address 'org.apache.dev'.
        Sends a message to org.apache.dev via router QDR.C and makes sure that the message was successfully
        routed (using partial address matching) and received using pre-created links that were created as a
        result of specifying addresses in the linkRoute attribute('org.apache.').
        """
        hello_world_1 = "Hello World_1!"

        # Connects to listener #2 on QDR.C
        addr = self.routers[2].addresses[0]

        blocking_connection = BlockingConnection(addr)

        # Receive on org.apache.dev
        blocking_receiver = blocking_connection.create_receiver(address="org.apache.dev")

        apply_options = AtMostOnce()

        # Sender to org.apache.dev
        blocking_sender = blocking_connection.create_sender(address="org.apache.dev", options=apply_options)
        msg = Message(body=hello_world_1)
        # Send a message
        blocking_sender.send(msg)

        received_message = blocking_receiver.receive()

        self.assertEqual(hello_world_1, received_message.body)

        # Connect to the router acting like the broker (QDR.A) and check the deliveriesIngress and deliveriesEgress
        local_node = Node.connect(self.routers[0].addresses[0], timeout=TIMEOUT)

        self.assertEqual(u'QDR.A', local_node.query(type='org.apache.qpid.dispatch.router',
                                                    attribute_names=[u'id']).results[0][0])

        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache.dev').deliveriesEgress)
        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache.dev').deliveriesIngress)

        # There should be 4 links -
        # 1. outbound receiver link on org.apache.dev
        # 2. inbound sender link on blocking_sender
        # 3. inbound link to the $management
        # 4. outbound link to $management
        # self.assertEqual(4, len()
        self.assertEquals(4, len(local_node.query(type='org.apache.qpid.dispatch.router.link').results))

        blocking_connection.close()

    def test_partial_link_route_match_1(self):
        """
        This test is pretty much the same as the previous test (test_partial_link_route_match) but the connection is
        made to router QDR.B instead of QDR.C and we expect to see the same behavior.
        """
        hello_world_2 = "Hello World_2!"
        addr = self.routers[1].addresses[0]

        blocking_connection = BlockingConnection(addr)

        # Receive on org.apache.dev
        blocking_receiver = blocking_connection.create_receiver(address="org.apache.dev")

        apply_options = AtMostOnce()

        # Sender to  to org.apache.dev
        blocking_sender = blocking_connection.create_sender(address="org.apache.dev", options=apply_options)
        msg = Message(body=hello_world_2)
        # Send a message
        blocking_sender.send(msg)

        received_message = blocking_receiver.receive()

        self.assertEqual(hello_world_2, received_message.body)

        local_node = Node.connect(self.routers[0].addresses[0], timeout=TIMEOUT)

        # Make sure that the router node acting as the broker (QDR.A) had one message routed through it. This confirms
        # that the message was link routed
        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache.dev').deliveriesEgress)

        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache.dev').deliveriesIngress)

        blocking_connection.close()

    def test_full_link_route_match(self):
        """
        The linkRoute on Routers C and B is set to org.apache.
        Creates a receiver listening on the address 'org.apache' and a sender that sends to address 'org.apache'.
        Sends a message to org.apache via router QDR.C and makes sure that the message was successfully
        routed (using full address matching) and received using pre-created links that were created as a
        result of specifying addresses in the linkRoute attribute('org.apache.').
        """
        hello_world_3 = "Hello World_3!"
        # Connects to listener #2 on QDR.C
        addr = self.routers[2].addresses[0]

        blocking_connection = BlockingConnection(addr)

        # Receive on org.apache
        blocking_receiver = blocking_connection.create_receiver(address="org.apache")

        apply_options = AtMostOnce()

        # Sender to  to org.apache
        blocking_sender = blocking_connection.create_sender(address="org.apache", options=apply_options)
        msg = Message(body=hello_world_3)
        # Send a message
        blocking_sender.send(msg)

        received_message = blocking_receiver.receive()

        self.assertEqual(hello_world_3, received_message.body)

        local_node = Node.connect(self.routers[0].addresses[0], timeout=TIMEOUT)

        # Make sure that the router node acting as the broker (QDR.A) had one message routed through it. This confirms
        # that the message was link routed
        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache').deliveriesEgress)

        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache').deliveriesIngress)

        blocking_connection.close()

    def _link_route_pattern_match(self, connect_node, include_host,
                                  exclude_host, test_address,
                                  expected_pattern):
        """
        This helper function ensures that messages sent to 'test_address' pass
        through 'include_host', and are *not* routed to 'exclude_host'

        """
        hello_pattern = "Hello Pattern!"
        route = 'M0' + test_address

        # Connect to the two 'waypoints', ensure the route is not present on
        # either

        node_A = Node.connect(include_host, timeout=TIMEOUT)
        node_B = Node.connect(exclude_host, timeout=TIMEOUT)

        for node in [node_A, node_B]:
            self.assertRaises(NotFoundStatus,
                              node.read,
                              type='org.apache.qpid.dispatch.router.address',
                              name=route)

        # wait until the host we're connecting to gets its next hop for the
        # pattern we're connecting to
        connect_node.wait_address(expected_pattern, remotes=1, delay=0.1)

        # Connect to 'connect_node' and send message to 'address'

        blocking_connection = BlockingConnection(connect_node.addresses[0])
        blocking_receiver = blocking_connection.create_receiver(address=test_address)
        blocking_sender = blocking_connection.create_sender(address=test_address,
                                                            options=AtMostOnce())
        msg = Message(body=hello_pattern)
        blocking_sender.send(msg)
        received_message = blocking_receiver.receive()
        self.assertEqual(hello_pattern, received_message.body)

        # verify test_address is only present on include_host and not on exclude_host

        self.assertRaises(NotFoundStatus,
                          node_B.read,
                          type='org.apache.qpid.dispatch.router.address',
                          name=route)

        self.assertEqual(1, node_A.read(type='org.apache.qpid.dispatch.router.address',
                                        name=route).deliveriesIngress)
        self.assertEqual(1, node_A.read(type='org.apache.qpid.dispatch.router.address',
                                        name=route).deliveriesIngress)

        # drop the connection and verify that test_address is no longer on include_host

        blocking_connection.close()

        timeout = time() + TIMEOUT
        while True:
            try:
                node_A.read(type='org.apache.qpid.dispatch.router.address',
                            name=route)
                if time() > timeout:
                    raise Exception("Expected route '%s' to expire!" % route)
                sleep(0.1)
            except NotFoundStatus:
                break;

        node_A.close()
        node_B.close()

    def test_link_route_pattern_match(self):
        """
        Verify the addresses match the proper patterns and are routed to the
        proper 'waypoint' only
        """
        qdr_A = self.routers[0].addresses[0]
        qdr_D = self.routers[3].addresses[0]
        qdr_C = self.routers[2]  # note: the node, not the address!

        self._link_route_pattern_match(connect_node=qdr_C,
                                       include_host=qdr_A,
                                       exclude_host=qdr_D,
                                       test_address='a.notD.toA',
                                       expected_pattern='a.*.toA.#')
        self._link_route_pattern_match(connect_node=qdr_C,
                                       include_host=qdr_D,
                                       exclude_host=qdr_A,
                                       test_address='a.notA.toD',
                                       expected_pattern='a.*.toD.#')
        self._link_route_pattern_match(connect_node=qdr_C,
                                       include_host=qdr_A,
                                       exclude_host=qdr_D,
                                       test_address='a.toD.toA.xyz',
                                       expected_pattern='a.*.toA.#')
        self._link_route_pattern_match(connect_node=qdr_C,
                                       include_host=qdr_D,
                                       exclude_host=qdr_A,
                                       test_address='a.toA.toD.abc',
                                       expected_pattern='a.*.toD.#')

    def test_custom_annotations_match(self):
        """
        The linkRoute on Routers C and B is set to org.apache.
        Creates a receiver listening on the address 'org.apache' and a sender that sends to address 'org.apache'.
        Sends a message with custom annotations to org.apache via router QDR.C and makes sure that the message was successfully
        routed (using full address matching) and received using pre-created links that were created as a
        result of specifying addresses in the linkRoute attribute('org.apache.'). Make sure custom annotations arrived as well.
        """
        hello_world_3 = "Hello World_3!"
        # Connects to listener #2 on QDR.C
        addr = self.routers[2].addresses[0]

        blocking_connection = BlockingConnection(addr)

        # Receive on org.apache
        blocking_receiver = blocking_connection.create_receiver(address="org.apache")

        apply_options = AtMostOnce()

        # Sender to  to org.apache
        blocking_sender = blocking_connection.create_sender(address="org.apache", options=apply_options)
        msg = Message(body=hello_world_3)
        annotations = {'custom-annotation': '1/Custom_Annotation'}
        msg.annotations = annotations

        # Send a message
        blocking_sender.send(msg)

        received_message = blocking_receiver.receive()

        self.assertEqual(hello_world_3, received_message.body)
        self.assertEqual(received_message.annotations, annotations)

        blocking_connection.close()

    def test_full_link_route_match_1(self):
        """
        This test is pretty much the same as the previous test (test_full_link_route_match) but the connection is
        made to router QDR.B instead of QDR.C and we expect the message to be link routed successfully.
        """
        hello_world_4 = "Hello World_4!"
        addr = self.routers[1].addresses[0]

        blocking_connection = BlockingConnection(addr)

        # Receive on org.apache
        blocking_receiver = blocking_connection.create_receiver(address="org.apache")

        apply_options = AtMostOnce()

        # Sender to  to org.apache
        blocking_sender = blocking_connection.create_sender(address="org.apache", options=apply_options)

        msg = Message(body=hello_world_4)
        # Send a message
        blocking_sender.send(msg)

        received_message = blocking_receiver.receive()

        self.assertEqual(hello_world_4, received_message.body)

        local_node = Node.connect(self.routers[0].addresses[0], timeout=TIMEOUT)

        # Make sure that the router node acting as the broker (QDR.A) had one message routed through it. This confirms
        # that the message was link routed
        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache').deliveriesEgress)

        self.assertEqual(1, local_node.read(type='org.apache.qpid.dispatch.router.address',
                                            name='M0org.apache').deliveriesIngress)

        blocking_connection.close()

    def test_zzz_qdmanage_delete_link_route(self):
        """
        We are deleting the link route using qdmanage short name. This should be the last test to run
        """

        local_node = Node.connect(self.routers[1].addresses[0], timeout=TIMEOUT)
        res = local_node.query(type='org.apache.qpid.dispatch.router')
        results = res.results[0]
        attribute_list = res.attribute_names

        result_list = local_node.query(type='org.apache.qpid.dispatch.router.config.linkRoute').results
        self.assertEqual(results[attribute_list.index('linkRouteCount')], len(result_list))

        # First delete linkRoutes on QDR.B
        for rid in range(8):
            cmd = 'DELETE --type=linkRoute --identity=' + result_list[rid][1]
            self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])

        cmd = 'QUERY --type=linkRoute'
        out = self.run_qdmanage(cmd=cmd, address=self.routers[1].addresses[0])
        self.assertEquals(out.rstrip(), '[]')

        # linkRoutes now gone on QDR.B but remember that it still exist on QDR.C
        # We will now try to create a receiver on address org.apache.dev on QDR.C.
        # Since the linkRoute on QDR.B is gone, QDR.C
        # will not allow a receiver to be created since there is no route to destination.

        # Connects to listener #2 on QDR.C
        addr = self.routers[2].addresses[0]

        # Now delete linkRoutes on QDR.C to eradicate linkRoutes completely
        local_node = Node.connect(addr, timeout=TIMEOUT)
        result_list = local_node.query(type='org.apache.qpid.dispatch.router.config.linkRoute').results

        # QDR.C has 8 link routes configured, nuke 'em:
        self.assertEqual(8, len(result_list))
        for rid in range(8):
            cmd = 'DELETE --type=linkRoute --identity=' + result_list[rid][1]
            self.run_qdmanage(cmd=cmd, address=addr)

        cmd = 'QUERY --type=linkRoute'
        out = self.run_qdmanage(cmd=cmd, address=addr)
        self.assertEquals(out.rstrip(), '[]')

        res = local_node.query(type='org.apache.qpid.dispatch.router')
        results = res.results[0]
        attribute_list = res.attribute_names
        self.assertEqual(results[attribute_list.index('linkRouteCount')], 0)

        blocking_connection = BlockingConnection(addr, timeout=3)

        # Receive on org.apache.dev (this address used to be linkRouted but not anymore since we deleted linkRoutes
        # on both QDR.C and QDR.B)
        blocking_receiver = blocking_connection.create_receiver(address="org.apache.dev")

        apply_options = AtMostOnce()
        hello_world_1 = "Hello World_1!"
        # Sender to org.apache.dev
        blocking_sender = blocking_connection.create_sender(address="org.apache.dev", options=apply_options)
        msg = Message(body=hello_world_1)

        # Send a message
        blocking_sender.send(msg)
        received_message = blocking_receiver.receive(timeout=5)
        self.assertEqual(hello_world_1, received_message.body)

    def test_yyy_delivery_tag(self):
        """
        Tests that the router carries over the delivery tag on a link routed delivery
        """
        listening_address = self.routers[1].addresses[1]
        sender_address = self.routers[2].addresses[0]
        qdstat_address = self.routers[2].addresses[0]
        test = DeliveryTagsTest(sender_address, listening_address, qdstat_address)
        test.run()
        self.assertEqual(None, test.error)

    def test_close_with_unsettled(self):
        test = CloseWithUnsettledTest(self.routers[1].addresses[0], self.routers[1].addresses[1])
        test.run()
        self.assertEqual(None, test.error)

    def test_www_drain_support_all_messages(self):
        drain_support = DrainMessagesHandler(self.routers[2].addresses[0])
        drain_support.run()
        self.assertEqual(None, drain_support.error)

    def test_www_drain_support_one_message(self):
        drain_support = DrainOneMessageHandler(self.routers[2].addresses[0])
        drain_support.run()
        self.assertEqual(None, drain_support.error)

    def test_www_drain_support_no_messages(self):
        drain_support = DrainNoMessagesHandler(self.routers[2].addresses[0])
        drain_support.run()
        self.assertEqual(None, drain_support.error)

    def test_www_drain_support_no_more_messages(self):
        drain_support = DrainNoMoreMessagesHandler(self.routers[2].addresses[0])
        drain_support.run()
        self.assertEqual(None, drain_support.error)

    def test_link_route_terminus_address(self):
        # The receiver is attaching to router B to a listener that has link route for address 'pulp.task' setup.
        listening_address = self.routers[1].addresses[1]
        # Run the query on a normal port
        query_address_listening = self.routers[1].addresses[0]

        # Sender is attaching to router C
        sender_address = self.routers[2].addresses[0]
        query_address_sending = self.routers[2].addresses[0]

        test = TerminusAddrTest(sender_address, listening_address, query_address_sending, query_address_listening)
        test.run()

        self.assertTrue(test.in_receiver_found)
        self.assertTrue(test.out_receiver_found)
        self.assertTrue(test.in_sender_found)
        self.assertTrue(test.out_sender_found)

    def test_dynamic_source(self):
        test = DynamicSourceTest(self.routers[1].addresses[0], self.routers[1].addresses[1])
        test.run()
        self.assertEqual(None, test.error)

    def test_dynamic_target(self):
        test = DynamicTargetTest(self.routers[1].addresses[0], self.routers[1].addresses[1])
        test.run()
        self.assertEqual(None, test.error)

    def test_detach_without_close(self):
        test = DetachNoCloseTest(self.routers[1].addresses[0], self.routers[1].addresses[1])
        test.run()
        self.assertEqual(None, test.error)

    def test_detach_mixed_close(self):
        test = DetachMixedCloseTest(self.routers[1].addresses[0], self.routers[1].addresses[1])
        test.run()
        self.assertEqual(None, test.error)

    def _multi_link_send_receive(self, send_host, receive_host, name):
        senders = ["%s/%s" % (send_host, address) for address in ["org.apache.foo", "org.apache.bar"]]
        receivers = ["%s/%s" % (receive_host, address) for address in ["org.apache.foo", "org.apache.bar"]]
        test = MultiLinkSendReceive(senders, receivers, name)
        test.run()
        self.assertEqual(None, test.error)

    def test_same_name_route_receivers_through_B(self):
        self._multi_link_send_receive(self.routers[0].addresses[0], self.routers[1].addresses[0], "recv_through_B")

    def test_same_name_route_senders_through_B(self):
        self._multi_link_send_receive(self.routers[1].addresses[0], self.routers[0].addresses[0], "send_through_B")

    def test_same_name_route_receivers_through_C(self):
        self._multi_link_send_receive(self.routers[0].addresses[0], self.routers[2].addresses[0], "recv_through_C")

    def test_same_name_route_senders_through_C(self):
        self._multi_link_send_receive(self.routers[2].addresses[0], self.routers[0].addresses[0], "send_through_C")

    def test_echo_detach_received(self):
        """
        Create two receivers to link routed address org.apache.dev
        Create a sender to the same address that the receiver is listening on and send 100 messages.
        After the receivers receive 10 messages each, the receivers will detach and expect to receive ten
        detaches in response.

        """
        test = EchoDetachReceived(self.routers[2].addresses[0], self.routers[2].addresses[0])
        test.run()
        self.assertEqual(None, test.error)


class Timeout(object):
    def __init__(self, parent):
        self.parent = parent

    def on_timer_task(self, event):
        self.parent.timeout()

class DeliveryTagsTest(MessagingHandler):
    def __init__(self, sender_address, listening_address, qdstat_address):
        super(DeliveryTagsTest, self).__init__()
        self.sender_address = sender_address
        self.listening_address = listening_address
        self.sender = None
        self.receiver_connection = None
        self.sender_connection = None
        self.qdstat_address = qdstat_address
        self.id = '1235'
        self.times = 1
        self.sent = 0
        self.rcvd = 0
        self.delivery_tag_verified = False
        # The delivery tag we are going to send in the transfer frame
        # We will later make sure that the same delivery tag shows up on the receiving end in the link routed case.
        # KAG: force the literal to type 'str' due to SWIG weirdness: on 2.X a
        # delivery tag cannot be unicode (must be binary), but on 3.X it must
        # be unicode!  See https://issues.apache.org/jira/browse/PROTON-1843
        self.delivery_tag = str('92319')
        self.error = None

    def timeout(self):
        self.error = "Timeout expired: sent=%d rcvd=%d" % (self.sent, self.rcvd)
        if self.receiver_connection:
            self.receiver_connection.close()
        if self.sender_connection:
            self.sender_connection.close()

    def on_start(self, event):
        self.timer               = event.reactor.schedule(TIMEOUT, Timeout(self))
        self.receiver_connection = event.container.connect(self.listening_address)

    def on_connection_remote_open(self, event):
        if event.connection == self.receiver_connection:
            continue_loop = True
            # Dont open the sender connection unless we can make sure that there is a remote receiver ready to
            # accept the message.
            # If there is no remote receiver, the router will throw a 'No route to destination' error when
            # creating sender connection.
            # The following loops introduces a wait before creating the sender connection. It gives time to the
            # router so that the address Dpulp.task can show up on the remoteCount
            i = 0
            while continue_loop:
                if i > 100: # If we have run the read command for more than hundred times and we still do not have
                    # the remoteCount set to 1, there is a problem, just exit out of the function instead
                    # of looping to infinity.
                    self.receiver_connection.close()
                    return
                local_node = Node.connect(self.qdstat_address, timeout=TIMEOUT)
                out = local_node.read(type='org.apache.qpid.dispatch.router.address', name='Dpulp.task').remoteCount
                if out == 1:
                    continue_loop = False
                else:
                    i += 1
                    sleep(0.25)

            self.sender_connection = event.container.connect(self.sender_address)
            self.sender = event.container.create_sender(self.sender_connection, "pulp.task", options=AtMostOnce())

    def on_sendable(self, event):
        if self.times == 1:
            msg = Message(body="Hello World")
            self.sender.send(msg, tag=self.delivery_tag)
            self.times += 1
            self.sent += 1

    def on_message(self, event):
        if "Hello World" == event.message.body:
            self.rcvd += 1

        # If the tag on the delivery is the same as the tag we sent with the initial transfer, it means
        # that the router has propagated the delivery tag successfully because of link routing.
        if self.delivery_tag != event.delivery.tag:
            self.error = "Delivery-tag: expected:%r got:%r" % (self.delivery_tag, event.delivery.tag)
        self.receiver_connection.close()
        self.sender_connection.close()
        self.timer.cancel()

    def run(self):
        Container(self).run()


class CloseWithUnsettledTest(MessagingHandler):
    ##
    ## This test sends a message across an attach-routed link.  While the message
    ## is unsettled, the client link is closed.  The test is ensuring that the
    ## router does not crash during the closing of the links.
    ##
    def __init__(self, normal_addr, route_addr):
        super(CloseWithUnsettledTest, self).__init__(prefetch=0, auto_accept=False)
        self.normal_addr = normal_addr
        self.route_addr  = route_addr
        self.dest = "pulp.task.CWUtest"
        self.error = None

    def timeout(self):
        self.error = "Timeout Expired - Check for cores"
        self.conn_normal.close()
        self.conn_route.close()

    def on_start(self, event):
        self.timer      = event.reactor.schedule(TIMEOUT, Timeout(self))
        self.conn_route = event.container.connect(self.route_addr)

    def on_connection_opened(self, event):
        if event.connection == self.conn_route:
            self.conn_normal = event.container.connect(self.normal_addr)
        elif event.connection == self.conn_normal:
            self.sender = event.container.create_sender(self.conn_normal, self.dest)

    def on_connection_closed(self, event):
        self.conn_route.close()
        self.timer.cancel()

    def on_link_opened(self, event):
        if event.receiver:
            self.receiver = event.receiver
            self.receiver.flow(1)

    def on_sendable(self, event):
        msg = Message(body="CloseWithUnsettled")
        event.sender.send(msg)

    def on_message(self, event):
        self.conn_normal.close()

    def run(self):
        Container(self).run()


class DynamicSourceTest(MessagingHandler):
    ##
    ## This test verifies that a dynamic source can be propagated via link-route to
    ## a route-container.
    ##
    def __init__(self, normal_addr, route_addr):
        super(DynamicSourceTest, self).__init__(prefetch=0, auto_accept=False)
        self.normal_addr = normal_addr
        self.route_addr  = route_addr
        self.dest = "pulp.task.DynamicSource"
        self.address = "DynamicSourceAddress"
        self.error = None

    def timeout(self):
        self.error = "Timeout Expired - Check for cores"
        self.conn_normal.close()
        self.conn_route.close()

    def on_start(self, event):
        self.timer      = event.reactor.schedule(TIMEOUT, Timeout(self))
        self.conn_route = event.container.connect(self.route_addr)

    def on_connection_opened(self, event):
        if event.connection == self.conn_route:
            self.conn_normal = event.container.connect(self.normal_addr)
        elif event.connection == self.conn_normal:
            self.receiver = event.container.create_receiver(self.conn_normal, None, dynamic=True,options=DynamicNodeProperties({"x-opt-qd.address":u"pulp.task.abc"}))

    def on_link_opened(self, event):
        if event.receiver == self.receiver:
            if self.receiver.remote_source.address != self.address:
                self.error = "Expected %s, got %s" % (self.address, self.receiver.remote_source.address)
            self.conn_normal.close()
            self.conn_route.close()
            self.timer.cancel()

    def on_link_opening(self, event):
        if event.sender:
            self.sender = event.sender
            if not self.sender.remote_source.dynamic:
                self.error = "Expected sender with dynamic source"
                self.conn_normal.close()
                self.conn_route.close()
                self.timer.cancel()
            self.sender.source.address = self.address
            self.sender.open()

    def run(self):
        Container(self).run()


class DynamicTarget(LinkOption):
    def apply(self, link):
        link.target.dynamic = True
        link.target.address = None


class DynamicTargetTest(MessagingHandler):
    ##
    ## This test verifies that a dynamic source can be propagated via link-route to
    ## a route-container.
    ##
    def __init__(self, normal_addr, route_addr):
        super(DynamicTargetTest, self).__init__(prefetch=0, auto_accept=False)
        self.normal_addr = normal_addr
        self.route_addr  = route_addr
        self.dest = "pulp.task.DynamicTarget"
        self.address = "DynamicTargetAddress"
        self.error = None

    def timeout(self):
        self.error = "Timeout Expired - Check for cores"
        self.conn_normal.close()
        self.conn_route.close()

    def on_start(self, event):
        self.timer      = event.reactor.schedule(TIMEOUT, Timeout(self))
        self.conn_route = event.container.connect(self.route_addr)

    def on_connection_opened(self, event):
        if event.connection == self.conn_route:
            self.conn_normal = event.container.connect(self.normal_addr)
        elif event.connection == self.conn_normal:
            self.sender = event.container.create_sender(self.conn_normal, None, options=\
                                                        [DynamicTarget(), DynamicNodeProperties({"x-opt-qd.address":u"pulp.task.abc"})])

    def on_link_opened(self, event):
        if event.sender == self.sender:
            if self.sender.remote_target.address != self.address:
                self.error = "Expected %s, got %s" % (self.address, self.receiver.remote_source.address)
            self.conn_normal.close()
            self.conn_route.close()
            self.timer.cancel()

    def on_link_opening(self, event):
        if event.receiver:
            self.receiver = event.receiver
            if not self.receiver.remote_target.dynamic:
                self.error = "Expected receiver with dynamic source"
                self.conn_normal.close()
                self.conn_route.close()
                self.timer.cancel()
            self.receiver.target.address = self.address
            self.receiver.open()

    def run(self):
        Container(self).run()


class DetachNoCloseTest(MessagingHandler):
    ##
    ## This test verifies that link-detach (not close) is propagated properly
    ##
    def __init__(self, normal_addr, route_addr):
        super(DetachNoCloseTest, self).__init__(prefetch=0, auto_accept=False)
        self.normal_addr = normal_addr
        self.route_addr  = route_addr
        self.dest = "pulp.task.DetachNoClose"
        self.error = None

    def timeout(self):
        self.error = "Timeout Expired - Check for cores"
        self.conn_normal.close()
        self.conn_route.close()

    def stop(self):
        self.conn_normal.close()
        self.conn_route.close()
        self.timer.cancel()

    def on_start(self, event):
        self.timer      = event.reactor.schedule(TIMEOUT, Timeout(self))
        self.conn_route = event.container.connect(self.route_addr)

    def on_connection_opened(self, event):
        if event.connection == self.conn_route:
            self.conn_normal = event.container.connect(self.normal_addr)
        elif event.connection == self.conn_normal:
            self.receiver = event.container.create_receiver(self.conn_normal, self.dest)

    def on_link_opened(self, event):
        if event.receiver == self.receiver:
            self.receiver.detach()

    def on_link_remote_detach(self, event):
        if event.sender == self.sender:
            self.sender.detach()
        if event.receiver == self.receiver:
            ##
            ## Test passed, we expected a detach on the propagated sender and back
            ##
            self.stop()

    def on_link_closing(self, event):
        if event.sender == self.sender:
            self.error = 'Propagated link was closed.  Expected it to be detached'
            self.stop()

        if event.receiver == self.receiver:
            self.error = 'Client link was closed.  Expected it to be detached'
            self.stop()

    def on_link_opening(self, event):
        if event.sender:
            self.sender = event.sender
            self.sender.source.address = self.sender.remote_source.address
            self.sender.open()

    def run(self):
        Container(self).run()


class DetachMixedCloseTest(MessagingHandler):
    ##
    ## This test verifies that link-detach (not close) is propagated properly
    ##
    def __init__(self, normal_addr, route_addr):
        super(DetachMixedCloseTest, self).__init__(prefetch=0, auto_accept=False)
        self.normal_addr = normal_addr
        self.route_addr  = route_addr
        self.dest = "pulp.task.DetachMixedClose"
        self.error = None

    def timeout(self):
        self.error = "Timeout Expired - Check for cores"
        self.conn_normal.close()
        self.conn_route.close()

    def stop(self):
        self.conn_normal.close()
        self.conn_route.close()
        self.timer.cancel()

    def on_start(self, event):
        self.timer      = event.reactor.schedule(TIMEOUT, Timeout(self))
        self.conn_route = event.container.connect(self.route_addr)

    def on_connection_opened(self, event):
        if event.connection == self.conn_route:
            self.conn_normal = event.container.connect(self.normal_addr)
        elif event.connection == self.conn_normal:
            self.receiver = event.container.create_receiver(self.conn_normal, self.dest)

    def on_link_opened(self, event):
        if event.receiver == self.receiver:
            self.receiver.detach()

    def on_link_remote_detach(self, event):
        if event.sender == self.sender:
            self.sender.close()
        if event.receiver == self.receiver:
            self.error = 'Client link was detached.  Expected it to be closed'
            self.stop()

    def on_link_closing(self, event):
        if event.sender == self.sender:
            self.error = 'Propagated link was closed.  Expected it to be detached'
            self.stop()

        if event.receiver == self.receiver:
            ##
            ## Test Passed
            ##
            self.stop()

    def on_link_opening(self, event):
        if event.sender:
            self.sender = event.sender
            self.sender.source.address = self.sender.remote_source.address
            self.sender.open()

    def run(self):
        Container(self).run()


# Test to validate fix for DISPATCH-927
class EchoDetachReceived(MessagingHandler):
    def __init__(self, sender_address, recv_address):
        super(EchoDetachReceived, self).__init__()
        self.sender_address = sender_address
        self.recv_address = recv_address
        self.dest = "org.apache.dev"
        self.num_msgs = 100
        self.num_receivers = 10
        self.msgs_sent = 0
        self.receiver_conn = None
        self.sender_conn = None
        self.sender = None
        self.receiver_dict = {}
        self.error = None
        self.receiver_attaches = 0
        self.timer = None
        self.sender_attached = False
        self.received_msgs_dict = {}
        self.receiver_detach_dict = {}
        self.num_detaches_echoed = 0

    @property
    def msgs_received(self):
        return sum(self.received_msgs_dict.values())

    def timeout(self):

        self.bail("Timeout Expired: msgs_sent=%d msgs_received=%d, number of detaches received=%d"
                  % (self.msgs_sent, self.msgs_received, self.num_detaches_echoed))

    def on_start(self, event):
        self.timer = event.reactor.schedule(TIMEOUT, Timeout(self))

        # Create two separate connections for sender and receivers
        self.receiver_conn = event.container.connect(self.recv_address)
        self.sender_conn = event.container.connect(self.sender_address)
        for i in range(self.num_receivers):
            name = "R%d" % i
            self.receiver_dict[name] = event.container.create_receiver(self.receiver_conn, self.dest, name=name)
            self.received_msgs_dict[name] = 0

    def bail(self, text=None):
        self.error = text
        self.sender_conn.close()
        self.receiver_conn.close()
        self.timer.cancel()

    def on_link_opened(self, event):
        if event.receiver:
            if event.receiver.name in list(self.receiver_dict):
                self.receiver_attaches+=1
            # The response receiver attaches have been received. The receiver sent attaches which was link routed
            # all the way to the 'broker' router and the response attaches have come back.
            # It is now time to create the sender.
            if self.receiver_attaches == self.num_receivers:
                self.sender = event.container.create_sender(self.sender_conn, self.dest)

        elif event.sender:
            if not self.sender_attached:
                if event.sender == self.sender:
                    # The sender attaches were link routed as well and the response attach has been received.
                    self.sender_attached = True

    def on_sendable(self, event):
        # The sender will send 100 messages
        if self.receiver_attaches == self.num_receivers and self.sender_attached:
            if self.msgs_sent < self.num_msgs:
                msg = Message(body="Hello World")
                self.sender.send(msg)
                self.msgs_sent += 1

    def on_message(self, event):
        if event.receiver and event.receiver.name in list(self.receiver_dict):
            self.received_msgs_dict[event.receiver.name] += 1

        if sum(self.received_msgs_dict.values()) == self.num_msgs:
            # The receivers have received a total of 100 messages. Close the receivers. The detach sent by these
            # receivers will travel all the way over the link route and the 'broker' router will respond with a
            # detach
            for receiver in list(self.receiver_dict):
                self.receiver_dict[receiver].close()

    def on_link_closed(self, event):
        if event.receiver.name in list(self.receiver_dict) and event.receiver.name not in list(self.receiver_detach_dict):
            self.receiver_detach_dict[event.receiver.name] = event.receiver
            self.num_detaches_echoed += 1

        # Terminate the test only if both detach frames have been received.
        if all(receiver in list(self.receiver_detach_dict) for receiver in list(self.receiver_dict)):
            self.bail()

    def run(self):
        Container(self).run()


class TerminusAddrTest(MessagingHandler):
    """
    This tests makes sure that the link route address is visible in the output of qdstat -l command.

    Sets up a sender on address pulp.task.terminusTestSender and a receiver on pulp.task.terminusTestReceiver.
    Connects to the router to which the sender is attached and makes sure that the pulp.task.terminusTestSender address
    shows up with an 'in' and 'out'
    Similarly connects to the router to which the receiver is attached and makes sure that the
    pulp.task.terminusTestReceiver address shows up with an 'in' and 'out'

    """
    def __init__(self, sender_address, listening_address, query_address_sending, query_address_listening):
        super(TerminusAddrTest, self).__init__()
        self.sender_address = sender_address
        self.listening_address = listening_address
        self.sender = None
        self.receiver = None
        self.message_received = False
        self.receiver_connection = None
        self.sender_connection = None
        # We will run a query on the same router where the sender is attached
        self.query_address_sending = query_address_sending

        # We will run a query on the same router where the receiver is attached
        self.query_address_listening = query_address_listening
        self.count = 0

        self.in_receiver_found = False
        self.out_receiver_found = False
        self.in_sender_found = False
        self.out_sender_found = False

        self.receiver_link_opened = False
        self.sender_link_opened = False

    def on_start(self, event):
        self.receiver_connection = event.container.connect(self.listening_address)

    def on_connection_remote_open(self, event):
        if event.connection == self.receiver_connection:
            continue_loop = True
            # The following loops introduces a wait. It gives time to the
            # router so that the address Dpulp.task can show up on the remoteCount
            i = 0
            while continue_loop:
                if i > 100: # If we have run the read command for more than hundred times and we still do not have
                    # the remoteCount set to 1, there is a problem, just exit out of the function instead
                    # of looping to infinity.
                    self.receiver_connection.close()
                    return
                local_node = Node.connect(self.query_address_sending, timeout=TIMEOUT)
                out = local_node.read(type='org.apache.qpid.dispatch.router.address', name='Dpulp.task').remoteCount
                if out == 1:
                    continue_loop = False
                i += 1
                sleep(0.25)

            self.sender_connection = event.container.connect(self.sender_address)

            # Notice here that the receiver and sender are listening on different addresses. Receiver on
            # pulp.task.terminusTestReceiver and the sender on pulp.task.terminusTestSender
            self.receiver = event.container.create_receiver(self.receiver_connection, "pulp.task.terminusTestReceiver")
            self.sender = event.container.create_sender(self.sender_connection, "pulp.task.terminusTestSender", options=AtMostOnce())

    def on_link_opened(self, event):
        if event.receiver == self.receiver:
            self.receiver_link_opened = True

            local_node = Node.connect(self.query_address_listening, timeout=TIMEOUT)
            out = local_node.query(type='org.apache.qpid.dispatch.router.link')

            link_dir_index = out.attribute_names.index("linkDir")
            owning_addr_index = out.attribute_names.index("owningAddr")

            # Make sure that the owningAddr M0pulp.task.terminusTestReceiver shows up on both in and out.
            # The 'out' link is on address M0pulp.task.terminusTestReceiver outgoing from the router B to the receiver
            # The 'in' link is on address M0pulp.task.terminusTestReceiver incoming from router C to router B
            for result in out.results:
                if result[link_dir_index] == 'in' and result[owning_addr_index] == 'M0pulp.task.terminusTestReceiver':
                    self.in_receiver_found = True
                if result[link_dir_index] == 'out' and result[owning_addr_index] == 'M0pulp.task.terminusTestReceiver':
                    self.out_receiver_found = True

        if event.sender == self.sender:
            self.sender_link_opened = True

            local_node = Node.connect(self.query_address_sending, timeout=TIMEOUT)
            out = local_node.query(type='org.apache.qpid.dispatch.router.link')

            link_dir_index = out.attribute_names.index("linkDir")
            owning_addr_index = out.attribute_names.index("owningAddr")

            # Make sure that the owningAddr M0pulp.task.terminusTestSender shows up on both in and out.
            # The 'in' link is on address M0pulp.task.terminusTestSender incoming from sender to router
            # The 'out' link is on address M0pulp.task.terminusTestSender outgoing from router C to router B
            for result in out.results:
                if result[link_dir_index] == 'in' and result[owning_addr_index] == 'M0pulp.task.terminusTestSender':
                    self.in_sender_found = True
                if result[link_dir_index] == 'out' and result[owning_addr_index] == 'M0pulp.task.terminusTestSender':
                    self.out_sender_found = True

        # Shutdown the connections only if the on_link_opened has been called for sender and receiver links.
        if self.sender_link_opened and self.receiver_link_opened:
            self.sender.close()
            self.receiver.close()
            self.sender_connection.close()
            self.receiver_connection.close()

    def run(self):
        Container(self).run()

class MultiLinkSendReceive(MessagingHandler):
    class SendState(object):
        def __init__(self, link):
            self.link = link
            self.sent = False
            self.accepted = False
            self.done = False
            self.closed = False

        def send(self, subject, body):
            if not self.sent:
                self.link.send(Message(subject=subject,body=body,address=self.link.target.address))
                self.sent = True

        def on_accepted(self):
            self.accepted = True
            self.done = True

        def close(self):
            if not self.closed:
                self.closed = True
                self.link.close()
                self.link.connection.close()

    class RecvState(object):
        def __init__(self, link):
            self.link = link
            self.received = False
            self.done = False
            self.closed = False

        def on_message(self):
            self.received = True
            self.done = True

        def close(self):
            if not self.closed:
                self.closed = True
                self.link.close()
                self.link.connection.close()

    def __init__(self, send_urls, recv_urls, name, message=None):
        super(MultiLinkSendReceive, self).__init__()
        self.send_urls = send_urls
        self.recv_urls = recv_urls
        self.senders = {}
        self.receivers = {}
        self.message = message or "SendReceiveTest"
        self.sent = False
        self.error = None
        self.name = name

    def close(self):
        for sender in self.senders.values():
            sender.close()
        for receiver in self.receivers.values():
            receiver.close()

    def all_done(self):
        for sender in self.senders.values():
            if not sender.done:
                return False
        for receiver in self.receivers.values():
            if not receiver.done:
                return False
        return True

    def timeout(self):
        self.error = "Timeout Expired"
        self.close()

    def stop_if_all_done(self):
        if self.all_done():
            self.stop()

    def stop(self):
        self.close()
        self.timer.cancel()

    def on_start(self, event):
        self.timer      = event.reactor.schedule(TIMEOUT, Timeout(self))
        event.container.container_id = None
        for u in self.send_urls:
            s = self.SendState(event.container.create_sender(u, name=self.name))
            self.senders[s.link.connection.container] = s
        for u in self.recv_urls:
            r = self.RecvState(event.container.create_receiver(u, name=self.name))
            self.receivers[r.link.connection.container] = r

    def on_sendable(self, event):
        self.senders[event.connection.container].send(self.name, self.message)

    def on_message(self, event):
        if self.message != event.message.body:
            error = "Incorrect message. Got %s, expected %s" % (event.message.body, self.message.body)
        self.receivers[event.connection.container].on_message()
        self.stop_if_all_done()

    def on_accepted(self, event):
        self.senders[event.connection.container].on_accepted()
        self.stop_if_all_done()

    def run(self):
        Container(self).run()


class LinkRouteContainerTest(TestCase):
    """
    Test link route implementation against custom containers

    Custom fake brokers (not a router) tailored for particular test cases.


             +-------------+         +---------+         +-----------------+
             |             | <------ |         | <-----  | blocking_sender |
             | fake broker |         |  QDR.A  |         +-----------------+
             |             | ------> |         | ------> +-------------------+
             +-------------+         +---------+         | blocking_receiver |
                                                         +-------------------+
    """
    @classmethod
    def setUpClass(cls):
        """Configure and start QDR.A"""
        super(LinkRouteContainerTest, cls).setUpClass()
        config = [
            ('router', {'mode': 'standalone', 'id': 'QDR.A'}),
            # for client connections:
            ('listener', {'role': 'normal',
                          'host': '0.0.0.0',
                          'port': cls.tester.get_port(),
                          'saslMechanisms': 'ANONYMOUS'}),
            # to connect to the fake broker
            ('connector', {'name': 'broker',
                           'role': 'route-container',
                           'host': '127.0.0.1',
                           'port': cls.tester.get_port(),
                           'saslMechanisms': 'ANONYMOUS'}),

            # forward 'org.apache' messages to + from fake broker:
            ('linkRoute', {'prefix': 'org.apache', 'containerId': 'FakeBroker', 'direction': 'in'}),
            ('linkRoute', {'prefix': 'org.apache', 'containerId': 'FakeBroker', 'direction': 'out'})
        ]
        config = Qdrouterd.Config(config)
        cls.router = cls.tester.qdrouterd('A', config, wait=False)

    def _fake_broker(self, cls):
        """Spawn a fake broker listening on the broker's connector
        """
        fake_broker = cls(self.router)
        # wait until the connection to the fake broker activates
        self.router.wait_connectors()
        return fake_broker

    def test_DISPATCH_1092(self):
        # This fake broker will force the session closed after the link
        # detaches.  Verify that the session comes back up correctly when the
        # next client attaches
        killer = self._fake_broker(SessionKiller)
        for i in range(2):
            bconn = BlockingConnection(self.router.addresses[0])
            bsender = bconn.create_sender(address="org.apache",
                                          options=AtMostOnce())
            msg = Message(body="Hey!")
            bsender.send(msg)
            bsender.close()
            bconn.close()
        killer.join()

    def test_delete_on_close(self):
        # create some deleteOnClose link routes:
        mgmt = self.router.management
        mgmt.create(type="org.apache.qpid.dispatch.router.config.linkRoute",
                    name="testy-in",
                    attributes={'prefix': 'testy',
                                'containerId': 'FakeBroker',
                                'direction': 'in',
                                'deleteOnClose': True})
        mgmt.create(type="org.apache.qpid.dispatch.router.config.linkRoute",
                    name="testy-out",
                    attributes={'prefix': 'testy',
                                'containerId': 'FakeBroker',
                                'direction': 'out',
                                'deleteOnClose': True})
        def count_link_routes():
            rsp = mgmt.query(type="org.apache.qpid.dispatch.router.config.linkRoute")
            total = count = 0
            for r in rsp.iter_dicts():
                total += 1
                if r.get("deleteOnClose", False):
                    count += 1
            return total, count

        self.assertEqual(4, count_link_routes()[0])
        self.assertEqual(2, count_link_routes()[1])
        broker = self._fake_broker(OnDeleteBroker)
        broker.done.wait(timeout=TIMEOUT)
        broker.join()
        self.assertEqual(2, count_link_routes()[0])
        self.assertEqual(0, count_link_routes()[1])


class _FakeBroker(MessagingHandler):
    """Base class for creating customized fake brokers.  This class spawns a
    broker thread that accepts incoming links and forwards based on the link
    address
    """
    def __init__(self, router):
        super(_FakeBroker, self).__init__()
        self.router = router
        self.address = router.connector_addresses[0]
        self.listener = None
        self._container = Container(self)
        self._container.container_id = 'FakeBroker'
        self._stop_thread = False
        self._connections = []
        self._senders = {}
        self._thread = Thread(target=self._main)
        self._thread.daemon = True
        self._thread.start()

    def _main(self):
        self._container.timeout = 1.0
        self._container.start()
        while self._container.process():
            if self._stop_thread:
                break
        if self.listener:
            self.listener.close()
        for c in self._connections:
            c.close()
        while self._container.process():
            pass
        self._container.stop()
        self._container.process()

    def join(self):
        # stop thread cleanly
        self._stop_thread = True
        self._container.wakeup()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise Exception("Fake Broker did not exit")

    def on_start(self, event):
        self.listener = event.container.listen(self.address)

    def on_connection_opening(self, event):
        # remotely initiated connection, need to set my id
        pn_conn = event.connection
        pn_conn.container = self._container.container_id

    def on_connection_opened(self, event):
        self._connections.append(event.connection)

    def on_connection_closed(self, event):
        self._connections.remove(event.connection)

    def on_link_opening(self, event):
        event.link.target.address = event.link.remote_target.address
        event.link.source.address = event.link.remote_source.address
        if event.link.is_sender:
            self._senders[event.link.remote_source.address] = event.link
        event.link.open()

    def on_message(self, event):
        out_link = self._senders.get(event.link.remote_target.address)
        out_link and out_link.send(event.message)


class SessionKiller(_FakeBroker):
    """DISPATCH-1092: force a session close when the link closes.  This should
    cause the router to re-create the session when the next client attaches.
    """
    def on_link_closing(self, event):
        event.link.close()
        event.session.close()


class OnDeleteBroker(_FakeBroker):
    """Test the automatic deletion of linkRoute configuration entities
    """
    def __init__(self, router):
        super(OnDeleteBroker, self).__init__(router)
        self._router_address = router.addresses[0]
        self._app_conn = None
        self._app_sender = None
        self._app_receiver = None
        self._app_sent = False
        self.done = Event()

    def on_connection_opened(self, event):
        super(OnDeleteBroker, self).on_connection_opened(event)
        if self._app_conn is None:
            # connector from router has been opened. Create application
            # sender/receiver
            self._app_conn = event.container.connect(self._router_address)
            self._app_receiver = event.container.create_receiver(self._app_conn,
                                                                 source="testy/one",
                                                                 name="app-receiver")
            self._app_sender = event.container.create_sender(self._app_conn,
                                                             target="testy/one",
                                                             name="app-sender")

    def on_sendable(self, event):
        if event.sender == self._app_sender and not self._app_sent:
            self._app_sent = True
            event.sender.send(Message(body="deleteOnClose"))
        else:
            super(OnDeleteBroker, self).on_sendable(event)

    def on_message(self, event):
        if event.receiver == self._app_receiver:
            self.done.set()
        else:
            super(OnDeleteBroker, self).on_message(event)


if __name__ == '__main__':
    unittest.main(main_module())
