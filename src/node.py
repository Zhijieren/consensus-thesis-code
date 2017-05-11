import Queue
import argparse
import logging
import random
import sys
from base64 import b64encode, b64decode

from twisted.internet import reactor, task
from twisted.internet.endpoints import TCP4ClientEndpoint, connectProtocol
from twisted.internet.error import CannotListenError
from twisted.internet.protocol import Factory
from typing import Dict, Tuple

from src.consensus.acs import ACS
from src.consensus.bracha import Bracha
from src.consensus.mo14 import Mo14
from src.trustchain.trustchain_runner import TrustChainRunner
from src.utils import Replay, Handled, set_logging, my_err_back, call_later, MAX_LINE_LEN
from src.utils.jsonreceiver import JsonReceiver
from .discovery import Discovery, got_discovery
import src.messages.messages_pb2 as pb


class MyProto(JsonReceiver):
    """
    Main protocol that handles the Byzantine consensus, one instance is created for each connection
    """
    def __init__(self, factory):
        self.factory = factory
        self.config = factory.config
        self.vk = factory.vk
        self.peers = factory.peers
        self.remote_vk = None
        self.state = 'SERVER'

    def connection_lost(self, reason):
        peer = "<None>" if self.remote_vk is None else b64encode(self.remote_vk)
        logging.debug("NODE: deleting peer {}".format(peer))
        try:
            del self.peers[self.remote_vk]
        except KeyError:
            logging.warning("NODE: peer {} already deleted".format(b64encode(self.remote_vk)))

    def obj_received(self, obj):
        """
        first we handle the items in the queue
        then we handle the received message
        :param obj:
        :return:
        """

        # logging.debug("NODE: received obj {} from {}".format(type(obj), "<remote_vk>"))

        # TODO do something like handler registry

        if isinstance(obj, pb.Ping):
            self.handle_ping(obj)

        elif isinstance(obj, pb.Pong):
            self.handle_pong(obj)

        elif isinstance(obj, pb.ACS):
            if self.factory.config.failure == 'omission':
                return
            res = self.factory.acs.handle(obj, self.remote_vk)
            self.process_acs_res(res, obj)

        elif isinstance(obj, pb.TxReq):
            self.factory.tc_runner.handle_tx_req(obj, self.remote_vk)

        elif isinstance(obj, pb.TxResp):
            self.factory.tc_runner.handle_tx_resp(obj, self.remote_vk)

        elif isinstance(obj, pb.ValidationReq):
            self.factory.tc_runner.handle_validation_req(obj, self.remote_vk)

        elif isinstance(obj, pb.ValidationResp):
            self.factory.tc_runner.handle_validation_resp(obj, self.remote_vk)

        elif isinstance(obj, pb.SigWithRound):
            self.factory.tc_runner.handle_sig(obj, self.remote_vk)

        elif isinstance(obj, pb.CpBlock):
            self.factory.tc_runner.handle_cp(obj, self.remote_vk)

        elif isinstance(obj, pb.Cons):
            self.factory.tc_runner.handle_cons(obj, self.remote_vk)

        elif isinstance(obj, pb.AskCons):
            self.factory.tc_runner.handle_ask_cons(obj, self.remote_vk)

        # NOTE messages below are for testing, bracha/mo14 is normally handled by acs

        elif isinstance(obj, pb.Bracha):
            if self.factory.config.failure == 'omission':
                return
            self.factory.bracha.handle(obj, self.remote_vk)

        elif isinstance(obj, pb.Mo14):
            if self.factory.config.failure == 'omission':
                return
            self.factory.mo14.handle(obj, self.remote_vk)

        elif isinstance(obj, pb.Dummy):
            logging.info("NODE: got dummy message from {}".format(b64encode(self.remote_vk)))

        else:
            raise AssertionError("invalid message type {}".format(obj))

    def process_acs_res(self, o, m):
        """

        :param o: the object we're processing
        :param m: the original message
        :return:
        """
        assert o is not None

        if isinstance(o, Replay):
            logging.debug("NODE: putting {} into msg queue".format(m))
            self.factory.q.put((self.remote_vk, m))
        elif isinstance(o, Handled):
            if self.factory.config.test == 'acs':
                logging.debug("NODE: testing ACS, not handling the result")
                return
            if o.m is not None:
                logging.debug("NODE: attempting to handle ACS result")
                self.factory.tc_runner.handle_cons_from_acs(o.m)
        else:
            raise AssertionError("instance is not Replay or Handled")

    def send_ping(self):
        self.send_obj(pb.Ping(vk=self.vk, port=self.config.port))
        logging.debug("NODE: sent ping")
        self.state = 'CLIENT'

    def handle_ping(self, msg):
        # type: (pb.Ping) -> None
        logging.debug("NODE: got ping, {}".format(msg))
        assert (self.state == 'SERVER')
        if msg.vk in self.peers.keys():
            logging.debug("NODE: ping found myself in peers.keys")
        self.peers[msg.vk] = (self.transport.getPeer().host, msg.port, self)
        self.remote_vk = msg.vk
        self.send_obj(pb.Pong(vk=self.vk, port=self.config.port))
        logging.debug("sent pong")

    def handle_pong(self, msg):
        # type: (pb.Pong) -> None
        logging.debug("NODE: got pong, {}".format(msg))
        assert (self.state == 'CLIENT')
        if msg.vk in self.peers.keys():
            logging.debug("NODE: pong: found myself in peers.keys")
            # self.transport.loseConnection()
        self.peers[msg.vk] = (self.transport.getPeer().host, msg.port, self)
        self.remote_vk = msg.vk
        logging.debug("NODE: done pong")


class MyFactory(Factory):
    """
    The Twisted Factory with a broadcast functionality, should be singleton
    """
    def __init__(self, config):
        # type: (Config) -> None
        self.peers = {}  # type: Dict[str, Tuple[str, int, MyProto]]
        self.promoters = []
        self.config = config
        self.bracha = Bracha(self)  # just for testing
        self.mo14 = Mo14(self)  # just for testing
        self.acs = ACS(self)
        self.tc_runner = TrustChainRunner(self)
        self.vk = self.tc_runner.tc.vk
        self.q = Queue.Queue()  # (str, msg)

        self._neighbour = None
        self._sorted_peer_keys = None

        # start looping call on the queue
        self.lc = task.LoopingCall(self.process_queue)
        self.lc.start(1).addErrback(my_err_back)

    def process_queue(self):
        # we use counter to stop this routine from running forever,
        # because self.json_received can put item back into the queue
        logging.debug("NODE: processing queue, size {}".format(self.q.qsize()))
        qsize = self.q.qsize()
        ctr = 0
        while ctr < qsize:
            ctr += 1
            node, m = self.q.get()
            self.peers[node][2].obj_received(m)

    def buildProtocol(self, addr):
        return MyProto(self)

    def new_connection_if_not_exist(self, nodes):
        for _vk, addr in nodes.iteritems():
            vk = b64decode(_vk)
            if vk not in self.peers.keys() and vk != self.vk:
                host, port = addr.split(":")
                self.make_new_connection(host, int(port))
            else:
                logging.debug("NODE: client {},{} already exist".format(b64encode(vk), addr))

    def make_new_connection(self, host, port):
        logging.debug("NODE: making client connection {}:{}".format(host, port))
        point = TCP4ClientEndpoint(reactor, host, port)
        proto = MyProto(self)
        d = connectProtocol(point, proto)
        d.addCallback(got_protocol).addErrback(my_err_back)

    def bcast(self, msg):
        """
        Broadcast a message to all nodes in self.peers, the list should include myself
        :param msg: dictionary that can be converted into json via send_json
        :return:
        """
        for k, v in self.peers.iteritems():
            proto = v[2]
            proto.send_obj(msg)

    def promoter_cast(self, msg):
        for promoter in self.promoters:
            self.send(promoter, msg)

    def promoter_cast_t(self, msg):
        for promoter in random.sample(self.promoters, self.config.t + 1):
            self.send(promoter, msg)

    def non_promoter_cast(self, msg):
        for node in set(self.peers.keys()) - set(self.promoters):
            self.send(node, msg)

    def gossip(self, msg):
        """
        Receivers of the gossiped currently needs to manually decide whether it wants to forward it.
        TODO create a special gossip message type and do the gossiping/forwarding on the base class.
        :param msg: 
        :return: 
        """
        fan_out = min(self.config.fan_out, self.config.n)
        for node in random.sample(self.peers.keys(), fan_out):
            self.send(node, msg)

    def gossip_except(self, exception, msg):
        new_set = set(self.peers.keys()) - set(exception)
        fan_out = min(self.config.fan_out, self.config.n, len(new_set))
        nodes = random.sample(new_set, fan_out)
        for node in nodes:
            self.send(node, msg)

    def multicast(self, nodes, msg):
        for node in nodes:
            self.send(node, msg)

    def send(self, node, msg):
        proto = self.peers[node][2]
        proto.send_obj(msg)

    def overwrite_promoters(self):
        """
        sets all peers to promoters, only use this method for testing
        :return:
        """
        logging.debug("NODE: overwriting promoters {}".format(len(self.peers)))
        self.promoters = self.peers.keys()

    @property
    def random_node(self):
        node = random.choice(self.peers.keys())
        while node == self.vk:
            node = random.choice(self.peers.keys())
        return node

    @property
    def random_non_promoter(self):
        if self.vk in self.promoters:
            return None

        node = random.choice(self.peers.keys())
        while node == self.vk or node in self.promoters:
            node = random.choice(self.peers.keys())
        return node

    @property
    def random_odd_node(self):
        node = random.choice(self.peers.keys())
        while node == self.vk or self.is_even_idx(node):
            node = random.choice(self.peers.keys())
        return node

    @property
    def neighbour(self):
        """
        Expect all peers to be connected, return the verification key of the node that's after me, or loop back
        :return: 
        """
        if self._neighbour is None:
            my_idx = self.sorted_peer_keys.index(self.vk)
            self._neighbour = self.sorted_peer_keys[(my_idx + 1) % len(self.sorted_peer_keys)]
        return self._neighbour

    def neighbour_if_even(self):
        """
        To maximise transaction rate, only nodes with an even index initiate TX.
        This function returns the neighbour if I'm on an even index otherwise None.
        :return: 
        """
        if self.is_even_idx(self.vk):
            return self.neighbour
        return None

    def is_even_idx(self, node):
        """
        Checks whether a node has even index
        :param node: 
        :return: 
        """
        idx = self.sorted_peer_keys.index(node)
        if idx % 2 == 0:
            return True
        return False

    @property
    def sorted_peer_keys(self):
        if self._sorted_peer_keys is None:
            self._sorted_peer_keys = sorted(self.peers.keys())
        return self._sorted_peer_keys

    def handle_instruction(self, msg):
        """
        The msg.delay need to be long enough such that the ping/pong messages are finished
        :param msg: 
        :return: 
        """
        assert isinstance(msg, pb.Instruction)
        logging.info("NODE: handling instruction {}".format(msg))

        call_later(msg.delay, self.tc_runner.bootstrap_promoters)

        if msg.instruction == 'bootstrap-only':
            pass

        elif msg.instruction == 'tx':
            rate = float(msg.param)
            interval = 1.0 / rate
            call_later(msg.delay, self.tc_runner.make_tx, interval, False)

        elif msg.instruction == 'tx-validate':
            rate = float(msg.param)
            interval = 1.0 / rate
            call_later(msg.delay, self.tc_runner.make_tx, interval, False)
            call_later(msg.delay + 10, self.tc_runner.make_validation, interval)

        elif msg.instruction == 'tx-random':
            rate = float(msg.param)
            interval = 1.0 / rate
            call_later(msg.delay, self.tc_runner.make_tx, interval, True)

        elif msg.instruction == 'tx-random-validate':
            rate = float(msg.param)
            interval = 1.0 / rate
            call_later(msg.delay, self.tc_runner.make_tx, interval, True)
            call_later(msg.delay + 10, self.tc_runner.make_validation, interval)

        else:
            raise AssertionError("Invalid instruction msg {}".format(msg))


def got_protocol(p):
    # this needs to be lower than the deferLater in `run`
    call_later(1, p.send_ping)


class Config(object):
    """
    All the static settings, used in Factory
    Should be singleton
    """
    def __init__(self, port, n, t, test, value, failure, tx_rate, consensus_delay, fan_out, validate, ignore_promoter):
        """
        This only stores the config necessary at runtime, so not necessarily all the information from argparse
        :param port:
        :param n:
        :param t:
        :param test:
        :param value:
        :param failure:
        :param tx_rate:
        :param consensus_delay:
        """
        self.port = port
        self.n = n
        self.t = t
        self.test = test

        assert value in (0, 1)
        self.value = value

        assert failure in ['byzantine', 'omission'] or failure is None
        self.failure = failure

        assert isinstance(tx_rate, float)
        self.tx_rate = tx_rate

        assert isinstance(consensus_delay, int)
        assert consensus_delay >= 0
        self.consensus_delay = consensus_delay

        self.fan_out = fan_out

        self.validate = validate

        self.ignore_promoter = ignore_promoter


def run(config, bcast, discovery_addr):
    JsonReceiver.MAX_LENGTH = MAX_LINE_LEN

    f = MyFactory(config)

    try:
        port = reactor.listenTCP(config.port, f)
        config.port = port.getHost().port
    except CannotListenError:
        logging.error("cannot listen on {}".format(config.port))
        sys.exit(1)

    # connect to discovery server
    point = TCP4ClientEndpoint(reactor, discovery_addr, 8123)
    d = connectProtocol(point, Discovery({}, f))
    d.addCallback(got_discovery, b64encode(f.vk), config.port).addErrback(my_err_back)

    # connect to myself
    point = TCP4ClientEndpoint(reactor, "localhost", config.port)
    d = connectProtocol(point, MyProto(f))
    d.addCallback(got_protocol).addErrback(my_err_back)

    if bcast:
        call_later(5, f.overwrite_promoters)

    # optionally run tests, args.test == None implies reactive node
    # we use call later to wait until the nodes are registered
    if config.test == 'dummy':
        call_later(5, f.bcast, pb.Dummy(m='z'))
    elif config.test == 'bracha':
        call_later(6, f.bracha.bcast_init)
    elif config.test == 'mo14':
        call_later(6, f.mo14.start, config.value)
    elif config.test == 'acs':
        # use port number (unique on local network) as test message
        call_later(6, f.acs.start, config.port, 1)
    elif config.test == 'tc':
        call_later(5, f.tc_runner.make_tx, 1.0 / config.tx_rate, True)
        # optionally use validate
        if config.validate:
            call_later(10, f.tc_runner.make_validation)
    elif config.test == 'bootstrap':
        call_later(5, f.tc_runner.bootstrap_promoters)

    logging.info("NODE: reactor starting on port {}".format(config.port))
    reactor.run()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'port',
        type=int, help='the listener port'
    )
    parser.add_argument(
        'n',
        type=int, help='the total number of promoters'
    )
    parser.add_argument(
        't',
        type=int, help='the total number of malicious nodes'
    )
    parser.add_argument(
        '-d', '--debug',
        help="log at debug level",
        action="store_const", dest="loglevel", const=logging.DEBUG,
        default=logging.WARNING
    )
    parser.add_argument(
        '-v', '--verbose',
        help="log at info level",
        action="store_const", dest="loglevel", const=logging.INFO
    )
    parser.add_argument(
        "-o", "--output",
        type=argparse.FileType('w'),
        metavar='NAME',
        help="location for the output file"
    )
    parser.add_argument(
        '--discovery',
        metavar='ADDR',
        default='localhost',
        help='address of the discovery server on port 8123'
    )
    parser.add_argument(
        '--consensus-delay',
        type=int,
        default=5,
        help='delay in seconds between consensus rounds'
    )
    parser.add_argument(
        '--fan-out',
        type=int,
        default=10,
        help='fan-out parameter for gossiping'
    )
    parser.add_argument(
        '--ignore-promoter',
        action='store_true',
        help='do not transact with promoters'
    )
    parser.add_argument(
        '--profile',
        metavar='NAME',
        help='run the node with cProfile'
    )
    parser.add_argument(
        '--test',
        choices=['dummy', 'bracha', 'mo14', 'acs', 'tc', 'bootstrap'],
        help='[testing] choose an algorithm to initialise'
    )
    parser.add_argument(
        '--value',
        choices=[0, 1],
        default=0,
        type=int,
        help='[testing] the initial input for BA'
    )
    parser.add_argument(
        '--failure',
        choices=['byzantine', 'omission'],
        help='[testing] the mode of failure'
    )
    parser.add_argument(
        '--tx-rate',
        type=float,
        metavar='RATE',
        default=0.0,
        help='[testing] initiate transaction at RATE/sec'
    )
    parser.add_argument(
        '--broadcast',
        help='[testing] overwrite promoters to be all peers',
        action='store_true'
    )
    parser.add_argument(
        '--validate',
        help="[testing] if test=='tc', perform validation",
        action='store_true'
    )
    args = parser.parse_args()

    set_logging(args.loglevel, args.output)

    def _run():
        run(Config(args.port, args.n, args.t, args.test, args.value, args.failure, args.tx_rate, args.consensus_delay,
                   args.fan_out, args.validate, args.ignore_promoter),
            args.broadcast, args.discovery)

    if args.profile:
        import cProfile
        cProfile.run('_run()', args.profile)
    else:
        _run()
