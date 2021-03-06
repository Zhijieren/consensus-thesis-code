import math
import libnacl
import copy
import logging
from base64 import b64encode
from typing import List, Union, Dict, Tuple, Optional
from enum import Enum

from src.utils import hash_pointers_ok, GrowingList, encode_n
import src.messages.messages_pb2 as pb

VALIDITY_ENUM = Enum('VALIDITY_ENUM', 'Valid Invalid Unknown')


class ProtobufWrapper(object):
    def __init__(self, x):
        """
        The argument `x`, or `self.pb`, is the only data that gets serialized.
        :param x: 
        """
        self.pb = x
        self._str = None
        self._hash = None

    def SerializeToString(self):
        if self._str is None:
            self._str = self.pb.SerializeToString()
        return self._str

    def __eq__(self, other):
        return self.SerializeToString() == other.SerializeToString()

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.SerializeToString())

    def __str__(self):
        return str(self.pb)

    @property
    def hash(self):
        # type: () -> str
        if self._hash is None:
            self._hash = libnacl.crypto_hash_sha256(self.SerializeToString())
        return self._hash


class Signature(ProtobufWrapper):
    """
    Data structure stores the verification key along with the signature,
    we expect the original message to be small, preferably a digest
    """
    def __init__(self, x):
        # type: (pb.Signature) -> None
        ProtobufWrapper.__init__(self, x)
        self.vk = self.pb.vk
        self._signed_document = self.pb.signed_document

    @classmethod
    def new(cls, vk, sk, msg):
        # type: (str, str, str) -> Signature
        return cls(pb.Signature(vk=vk, signed_document=libnacl.crypto_sign(msg, sk)))


    def verify(self, vk, msg):
        # type: (str, str) -> None
        """
        Throws ValueError on failure
        :return:
        """
        if vk != self.vk:
            raise ValueError("Mismatch verification key")
        expected_msg = libnacl.crypto_sign_open(self._signed_document, self.vk)
        if expected_msg != msg:
            raise ValueError("Mismatch message")


class TxBlock(ProtobufWrapper):
    def __init__(self, x):
        # type: (pb.TxBlock) -> None
        """
        Convert a protobuf TxBlock into a TrustChain TxBlock
        :param x: 
        """
        ProtobufWrapper.__init__(self, x)
        self.inner = self.pb.inner
        self.s = Signature(self.pb.s)

        # properties below are not a part of hash
        self.other_half = None
        self.validity = VALIDITY_ENUM.Unknown
        self.request_sent_r = -1  # a positive value indicate the round at which the request is sent

        # make sure the arguments of CompactBlock constructor are initialised, especially _tuple
        self.compact = CompactBlock.new(self.hash, self.prev, self.seq)

    @classmethod
    def new(cls, prev, seq, counterparty, m, vk, sk, nonce=None):
        # type: (str, int, str, str, str, str, str) -> pb.TxBlock
        if nonce is None:
            nonce = libnacl.randombytes(32)
        inner = pb.TxBlock.Inner(prev=prev, seq=seq, counterparty=counterparty, nonce=nonce, m=m)
        s = Signature.new(vk, sk, libnacl.crypto_hash_sha256(inner.SerializeToString()))
        return cls(pb.TxBlock(inner=inner, s=s.pb))

    @property
    def seq(self):
        # type: () -> int
        return self.inner.seq

    @property
    def prev(self):
        # type: () -> str
        return self.inner.prev

    def add_other_half(self, other_half):
        # type: (TxBlock) -> ()
        assert self.inner.nonce == other_half.inner.nonce
        assert self.inner.m == other_half.inner.m
        other_half.s.verify(self.inner.counterparty,
                            libnacl.crypto_hash_sha256(other_half.inner.SerializeToString()))
        self.other_half = other_half


def _verify_signatures(data, ss, vks, t):
    # type: (str, List[Signature], List[str], int) -> None
    _ss = [s for s in ss if s.vk in vks]  # only consider nodes that are promoters

    # validation will surely fail if these are not satisfied
    if not len(ss) > t:
        raise ValueError("{} > {}, not satisfied".format(len(ss), t))

    if not len(_ss) > t:
        raise ValueError("{} > {}, not satisfied".format(len(_ss), t))

    oks = 0
    for _s in _ss:
        try:
            _s.verify(_s.vk, data)
            oks += 1
        except ValueError:
            logging.debug("one verification failed for {}".format(_s.vk))

    if not oks > t:
        raise ValueError("verification failed, oks = {}, t = {}".format(oks, t))


class CpBlock(ProtobufWrapper):
    """
    In the network, a CpBlock should be created using the following steps
    1, node receives some consensus result
    2, node receives some signatures
    3, generate the cp block
    """
    def __init__(self, x):
        ProtobufWrapper.__init__(self, x)
        self.inner = self.pb.inner
        self.s = Signature(self.pb.s)  # type: Signature

        self.compact = CompactBlock.new(self.hash, self.prev, self.seq)

    @classmethod
    def new(cls, prev, seq, cons, p, vk, sk, ss, vks, t):
        # type: (str, int, Cons, int, str, str, List[Signature], List[str], int) -> pb.CpBlock
        """

        :param prev: hash pointer to the previous block
        :param cons: type Cons
        :param seq: height
        :param p: promoter flag
        :param vk: my verification key
        :param sk: my secret key
        :param ss: signatures of the promoters, at least t-1 of them must be valid
        :param vks: all verification keys of promoters
        :param t:
        """
        assert p in (0, 1)
        inner = pb.CpBlock.Inner(prev=prev, seq=seq, round=cons.round, cons_hash=cons.hash, ss=[s.pb for s in ss], p=p)

        if cons.round != 0 or len(ss) != 0 or len(vks) != 0 or inner.seq != 0:
            _verify_signatures(inner.cons_hash, ss, vks, t)
        else:
            # if this is executed, it means this is a genesis block
            pass
        s = Signature.new(vk, sk, libnacl.crypto_hash_sha256(inner.SerializeToString()))

        return cls(pb.CpBlock(inner=inner, s=s.pb))

    @property
    def luck(self):
        # type: () -> str
        return libnacl.crypto_hash_sha256(self.hash + self.s.vk)

    @property
    def seq(self):
        # type: () -> int
        return self.inner.seq

    @property
    def prev(self):
        # type: () -> str
        return self.inner.prev

    @property
    def round(self):
        # type: () -> int
        return self.inner.round


class CompactBlock(ProtobufWrapper):
    def __init__(self, x):
        # type: (pb.CompactBlock) -> None
        ProtobufWrapper.__init__(self, x)
        self.digest = self.pb.inner.digest
        self.prev = self.pb.inner.prev

        # NOTE: these will be mutated, so we use getter/setter
        self._seq = self.pb.seq
        self._agreed_round = self.pb.agreed_round

    @classmethod
    def new(cls, digest, prev, seq):
        # type: (str, str, int) -> pb.CompactBlock
        return cls(pb.CompactBlock(inner=pb.CompactBlock.Inner(digest=digest, prev=prev), seq=seq, agreed_round=-1))

    @property
    def seq(self):
        return self._seq

    @seq.setter
    def seq(self, v):
        self.pb.seq = v
        self._seq = self.pb.seq

    @property
    def agreed_round(self):
        return self._agreed_round

    @agreed_round.setter
    def agreed_round(self, v):
        self.pb.agreed_round = v
        self._agreed_round = self.pb.agreed_round

    @property
    def hash(self):
        # type: () -> str
        """
        Override the normal hash, we only need the inner part
        :return: 
        """
        if self._hash is None:
            self._hash = libnacl.crypto_hash_sha256(self.pb.inner.SerializeToString())
        return self._hash


class Cons(ProtobufWrapper):
    """
    The consensus results, data structure that the promoters agree on
    """
    def __init__(self, x):
        # type: (pb.Cons) -> None
        ProtobufWrapper.__init__(self, x)
        self.round = self.pb.round
        self.blocks = [CpBlock(blk) for blk in self.pb.blocks]  # convert to TrustChain type
        self._promoters = []

    @classmethod
    def new(cls, round, blocks):
        # type: (int, List[pb.CpBlock]) -> pb.Cons
        """
        NOTE: this new function is a bit different from others, it accepts a Protobuf type,
        because it gets used directly in init.
        :param round: consensus round
        :param blocks: list of agreed checkpoint blocks
        """
        return cls(pb.Cons(round=round, blocks=blocks))

    def get_promoters(self, n):
        # type: () -> List[str]
        if not self._promoters:
            registered = [cp for cp in self.blocks if cp.inner.p == 1]
            registered.sort(key=lambda x: x.luck)
            self._promoters = [b.s.vk for b in registered][:n]
        return self._promoters

    @property
    def count(self):
        # type: () -> int
        return len(self.blocks)


def generate_genesis_block(vk, sk):
    # type: (str, str) -> CpBlock
    prev = libnacl.crypto_hash_sha256('0')
    cons = Cons.new(0, [])
    return CpBlock.new(prev, 0, cons, 1, vk, sk, [], [], 0)


class Chain(object):
    def __init__(self, vk, sk):
        # type: (str, str) -> None
        self.vk = vk
        self.chain = [generate_genesis_block(vk, sk)]  # type: List[Union[CpBlock, TxBlock]]
        self._tx_count = 0
        self._cp_count = 0
        self.latest_cp = self.chain[0]

    def new_tx(self, tx):
        # type: (TxBlock) -> None
        assert tx.prev == self.chain[-1].compact.hash
        assert tx.seq == self.chain[-1].seq + 1

        self.chain.append(tx)
        self._tx_count += 1

    def new_cp(self, cp):
        # type: (CpBlock) -> None
        assert cp.prev == self.chain[-1].compact.hash
        assert cp.seq == self.chain[-1].seq + 1

        prev_cp = self.latest_cp
        assert prev_cp.inner.round < cp.inner.round, \
            "prev round {}, curr round {}, len {}".format(prev_cp, cp, len(self.chain))

        self.chain.append(cp)
        self._cp_count += 1

        self.latest_cp = cp

    def get_cp_of_round(self, r):
        # type: (int) -> Optional[CpBlock]
        # TODO an optimisation would be to begin from the back side if 'r' is large
        for block in self.chain:
            if isinstance(block, CpBlock):
                if block.round == r:
                    return block
        return None

    @property
    def latest_compact_hash(self):
        # type: () -> str
        return self.chain[-1].compact.hash

    @property
    def latest_hash(self):
        # type: () -> str
        return self.chain[-1].hash

    @property
    def genesis(self):
        # type: () -> CpBlock
        return self.chain[0]

    @property
    def latest_round(self):
        # type: () -> int
        return self.latest_cp.inner.round

    @property
    def tx_count(self):
        # type: () -> int
        return self._tx_count

    @property
    def cp_count(self):
        # type: () -> int
        return self._cp_count

    def pieces(self, seq):
        # type: (int) -> List[CompactBlock]
        """
        tx must exist, return the pieces of tx
        :param seq:
        :return: List<Block>
        """
        c_a, c_b = self._enclosure(seq)
        if c_a is None or c_b is None:
            return []

        # the height (h) should always be correct, since it is checked when adding new CP
        return [b.compact for b in self.chain[c_a.seq:c_b.seq + 1]]

    def _enclosure(self, seq):
        # type: (int) -> Tuple[CpBlock, CpBlock]
        """
        Finds two CP blocks that encloses a TX block with a sequence number `seq`
        :param seq: the sequence number of interest, must be a TX block
        :return: (CpBlock, CpBlock)
        """
        tx = self.chain[seq]
        assert isinstance(tx, TxBlock)

        cp_a = cp_b = None

        for i in xrange(seq - 1, -1, -1):
            cp = self.chain[i]
            if isinstance(cp, CpBlock):
                cp_a = cp
                break

        for i in xrange(seq + 1, len(self.chain)):
            cp = self.chain[i]
            if isinstance(cp, CpBlock):
                cp_b = cp
                break

        return cp_a, cp_b

    def set_validity(self, seq, validity):
        # type: (int, VALIDITY_ENUM) -> None
        """
        Set the validity of a tx block, if it's already Valid or Invalid, it cannot be changed.
        :param seq: 
        :param validity: 
        :return: 
        """
        tx = self.chain[seq]
        assert isinstance(tx, TxBlock)
        assert validity != VALIDITY_ENUM.Unknown

        if tx.validity == VALIDITY_ENUM.Unknown:
            tx.validity = validity

    def get_unknown_txs(self):
        # type: () -> List[TxBlock]
        """
        Return a list of TXs which have unknown validity
        :return: 
        """
        def _is_valid(b):
            return isinstance(b, TxBlock) and b.validity == VALIDITY_ENUM.Unknown and b.other_half is not None
        return filter(_is_valid, self.chain)

    def get_validated_txs(self):
        # type: () -> List[TxBlock]
        """
        Opposite of `get_unknown_txs`
        :return: 
        """
        return filter(lambda b: isinstance(b, TxBlock) and b.validity != VALIDITY_ENUM.Unknown, self.chain)

    def compute_latest_cp(self):
        # type: () -> CpBlock
        for i in xrange(len(self.chain) - 1, -1, -1):
            b = self.chain[i]
            if isinstance(b, CpBlock):
                return b
        raise ValueError("No CpBlock in Chain")


class TrustChain(object):
    """
    Node maintains one TrustChain object and interacts with it either in in the reactor process or some other process.
    If it's the latter, there needs to be some communication mechanism.

    We assume there's a keyserver, so public keys (vk) of all nodes are available to us.
    """

    def __init__(self):
        # type: () -> None
        self.vk, self._sk = libnacl.crypto_sign_keypair()
        self._other_chains = {}  # type: Dict[str, GrowingList]
        self.my_chain = Chain(self.vk, self._sk)
        self.consensus = {}  # type: Dict[int, Cons]
        logging.info("TC: my VK is {}".format(b64encode(self.vk)))

    def new_tx(self, counterparty, m, nonce=None):
        # type: (str, str, str) -> None
        """
        
        :param counterparty: 
        :param m: 
        :param nonce: 
        :return: 
        """
        tx = TxBlock.new(self.latest_compact_hash, self.next_seq, counterparty, m, self.vk, self._sk, nonce)
        self._new_tx(tx)

    def _new_tx(self, tx):
        # type: (TxBlock) -> None
        """
        Verify tx, follow the rules and mutates the state to add it
        :return: None
        """
        assert tx.seq == self.next_seq, "{} != {}".format(tx.seq, self.next_seq)
        self.my_chain.new_tx(copy.deepcopy(tx))

    def new_cp(self, p, cons, ss, vks, t):
        # type: (int, Cons, List[Signature], List[str], int) -> None
        """

        :param p:
        :param cons:
        :param ss: signature of the promoters
        :param vks: verification key of the promoters
        :param t:
        :return:
        """
        assert cons.round not in self.consensus
        self.consensus[cons.round] = cons
        cp = CpBlock.new(self.latest_compact_hash, self.next_seq, cons, p, self.vk, self._sk, ss, vks, t)
        self._new_cp(cp)

    def _new_cp(self, cp):
        # type: (CpBlock) -> None
        """
        Verify the cp, follow the rules and mutate the state to add it
        NOTE: this does not cache the consensus result
        :return: None
        """
        assert cp.seq == len(self.my_chain.chain)
        self.my_chain.new_cp(copy.deepcopy(cp))

    @property
    def next_seq(self):
        # type: () -> int
        return len(self.my_chain.chain)

    @property
    def latest_hash(self):
        # type: () -> str
        return self.my_chain.latest_hash

    @property
    def latest_compact_hash(self):
        # type: () -> str
        return self.my_chain.latest_compact_hash

    @property
    def genesis(self):
        # type: () -> CpBlock
        return self.my_chain.genesis

    @property
    def latest_round(self):
        # type: () -> int
        return self.my_chain.latest_round

    @property
    def tx_count(self):
        # type: () -> int
        return self.my_chain.tx_count

    @property
    def cp_count(self):
        # type: () -> int
        return self.my_chain.cp_count

    @property
    def latest_cp(self):
        # type: () -> CpBlock
        return self.my_chain.latest_cp

    def consensus_round_of_cp(self, cp):
        # type: (CpBlock) -> int
        """
        Given a CP, find the consensus round that contains it
        :param cp: 
        :return: 
        """
        assert isinstance(cp, CpBlock)
        for r in range(cp.round, self.my_chain.latest_round + 1):
            if r in self.consensus:
                if any(map(lambda b: b.hash == cp.hash, self.consensus[r].blocks)):
                    return r
        return -1

    def compact_cp_in_consensus(self, cp, r):
        # type: (CompactBlock) -> bool
        if r not in self.consensus:
            return False
        cons = self.consensus[r]
        return any(map(lambda b: b.compact.hash == cp.hash, cons.blocks))

    def pieces(self, seq):
        # type: (int) -> List[CompactBlock]
        return self.my_chain.pieces(seq)

    def agreed_pieces(self, seq):
        # type: (int) -> List[CompactBlock]
        c_a, c_b, r_a, r_b = self._agreed_enclosure(seq)
        if c_a is None or c_b is None or r_a == -1 or r_b == -1:
            return []

        # the height (h) should always be correct, since it is checked when adding new CP
        blocks = [b.compact for b in self.my_chain.chain[c_a.seq:c_b.seq + 1]]
        blocks[0].agreed_round = r_a
        blocks[-1].agreed_round = r_b
        return blocks

    def _agreed_enclosure(self, seq):
        # type: (int) -> Tuple[Optional[CpBlock], Optional[CpBlock], int, int]
        """
        search backward for an agreed piece
        search forward for an agreed pieces
        get the agreed enclosure
        get the agreed pieces
        :param seq: 
        :return: 
        """

        tx = self.my_chain.chain[seq]
        assert isinstance(tx, TxBlock)

        cp_a = cp_b = None
        r_a = r_b = -1

        for i in xrange(seq - 1, -1, -1):
            cp = self.my_chain.chain[i]
            if isinstance(cp, CpBlock):
                r_a = self.consensus_round_of_cp(cp)
                if r_a != -1:
                    cp_a = cp
                    break

        for i in xrange(seq + 1, len(self.my_chain.chain)):
            cp = self.my_chain.chain[i]
            if isinstance(cp, CpBlock):
                r_b = self.consensus_round_of_cp(cp)
                if r_b != -1:
                    cp_b = cp
                    break

        return cp_a, cp_b, r_a, r_b

    def load_cache_for_verification(self, seq):
        # type: (int) -> List[CompactBlock]
        """
        Check that we can verify using cached results, 
        :param seq: 
        :return: 
        """
        tx = self.my_chain.chain[seq]
        assert isinstance(tx, TxBlock)
        assert tx.other_half is not None

        if tx.inner.counterparty not in self._other_chains:
            return []

        blocks_cache = self._other_chains[tx.inner.counterparty]
        other_seq = tx.other_half.seq

        if len(blocks_cache) <= other_seq:
            return []

        if blocks_cache[other_seq] is None:
            return []

        # iterate starting from other_seq (both sides)
        # check that there exist some agreed_round

        idx_a = -1
        idx_b = -1

        for i in xrange(other_seq - 1, -1, -1):
            block = blocks_cache[i]
            if block is None:
                return []
            if block.agreed_round != -1:
                idx_a = i
                break

        for i in xrange(other_seq + 1, len(blocks_cache)):
            block = blocks_cache[i]
            if block is None:
                return []
            if block.agreed_round != -1:
                idx_b = i
                break

        if idx_a == -1 or idx_b == -1:
            return []

        return blocks_cache[idx_a:idx_b+1]

    def verify_tx(self, seq, compact_blocks, use_cache=True):
        # type: (int, List[CompactBlock]) -> VALIDITY_ENUM
        """
        We want to verify one of our own TX with expected round numbers that contains the consensus result of the piece
        and the sequence number (height) `seq` that contains the pair
        against some result `resp` we got from the counterparty.

        If successful, we store the compact_chain in cache.
        :param seq:
        :param compact_blocks:
        :return:
        """
        if compact_blocks is None:
            raise NotImplemented

        tx = self.my_chain.chain[seq]
        assert isinstance(tx, TxBlock)
        assert tx.other_half is not None

        if len(compact_blocks) == 0:
            return VALIDITY_ENUM.Unknown

        # check that I also have the same CP blocks
        # hash pointers are ok
        # check the pair is in the received blocks
        # TODO what about the round diff?

        peer_cp_a = compact_blocks[0]
        peer_cp_b = compact_blocks[-1]
        r_a = peer_cp_a.agreed_round
        r_b = peer_cp_b.agreed_round
        assert isinstance(peer_cp_a, CompactBlock)
        assert isinstance(peer_cp_b, CompactBlock)

        if not (self.compact_cp_in_consensus(peer_cp_a, r_a) and self.compact_cp_in_consensus(peer_cp_b, r_b)):
            return VALIDITY_ENUM.Unknown

        if not hash_pointers_ok(compact_blocks):
            return VALIDITY_ENUM.Unknown

        # TODO the logic here is ugly and error prone
        for b in compact_blocks:
            if b.hash == tx.other_half.compact.hash:
                assert b.seq == tx.other_half.seq
                self.my_chain.set_validity(seq, VALIDITY_ENUM.Valid)
                logging.debug("TC: verified {}".format(encode_n(self.my_chain.chain[seq].hash)))
                if use_cache:
                    updated = self._cache_compact_blocks(tx.inner.counterparty, compact_blocks)
                    if updated:
                        self._verify_from_cache(tx.inner.counterparty)
                return VALIDITY_ENUM.Valid

        return VALIDITY_ENUM.Unknown

    def _cache_compact_blocks(self, vk, compact_blocks):
        # type: (str, List[CompactBlock]) -> bool
        updated = False

        if vk not in self._other_chains:
            self._other_chains[vk] = GrowingList()

        blocks_cache = self._other_chains[vk]

        idx = compact_blocks[0].seq
        for compact_block in compact_blocks:
            assert idx == compact_block.seq
            # blocks_cache[idx] can be none of we skip a segment of the cache
            if len(blocks_cache) > idx:
                if blocks_cache[idx] is None:
                    blocks_cache[idx] = compact_block
                    updated = True
                else:
                    assert blocks_cache[idx] == compact_block
            else:
                blocks_cache[idx] = compact_block
                updated = True
            idx += 1

        self._other_chains[vk] = blocks_cache

        return updated

    def _verify_from_cache(self, counterparty):
        """
        This function should be called every time the cache is updated,
        and then verify all tx that belongs to counterparty.
        :param counterparty: 
        :return: 
        """
        txs = filter(lambda _tx: _tx.inner.counterparty == counterparty, self.get_verifiable_txs())
        for tx in txs:
            compact_blocks = self.load_cache_for_verification(tx.seq)
            res = self.verify_tx(tx.seq, compact_blocks, use_cache=False)
            if res == VALIDITY_ENUM.Valid:
                logging.debug("TC: verified (from cache) {}".format(encode_n(tx.hash)))

    def get_verifiable_txs(self):
        # type: () -> List[TxBlock]
        """
        There are some transactions that are impossible to verify because we don't have the consensus result,
        or the validation request is already sent but we haven't heard the reply,
        this function attempts to filter these cases.
        :return: 
        """
        if self.latest_cp.round < 2:
            return []
        max_h = self.my_chain.get_cp_of_round(self.latest_cp.round - 1).seq
        txs = filter(lambda _tx: _tx.seq < max_h and _tx.request_sent_r < self.latest_round,
                     self.my_chain.get_unknown_txs())
        return txs

    def get_validated_txs(self):
        # type: () -> List[TxBlock]
        return self.my_chain.get_validated_txs()

# EqHash.register(Signature)
# EqHash.register(TxBlockInner)
# EqHash.register(TxBlock)
# EqHash.register(CpBlockInner)
# EqHash.register(CpBlock)
# EqHash.register(Cons)

# if __name__ == '__main__':
#     vk, sk = libnacl.crypto_sign_keypair()
#     s = Signature(vk, sk, "test")
#     s.verify(vk, "test")
#
#     generate_genesis_block(vk, sk)
