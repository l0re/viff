# Copyright 2007 Martin Geisler
#
# This file is part of VIFF
#
# VIFF is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# VIFF is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with VIFF in the file COPYING; if not, write to the Free
# Software Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA
# 02110-1301 USA

"""VIFF runtime.

This is where the virtual ideal functionality is hiding! The runtime
is responsible for sharing inputs, handling communication, and running
the calculations.

Each player participating in the protocol will instantiate a
L{Runtime} object and use it for the calculations.
"""

import marshal
import socket

from viff import shamir
from viff.prss import prss
from viff.field import GF, GF256, FieldElement
from viff.util import rand, dprint, println, clone_deferred

from twisted.internet import defer, reactor
from twisted.internet.defer import Deferred, DeferredList, gatherResults
from twisted.internet.protocol import ClientFactory, ServerFactory
from twisted.protocols.basic import Int16StringReceiver


class Share(Deferred):
    def __init__(self, runtime, value=None):
        Deferred.__init__(self)
        self.runtime = runtime
        if value is not None:
            self.callback(value)

    def __add__(self, other):
        return self.runtime.add(self, other)

    def __radd__(self, other):
        return self.runtime.add(other, self)

    def __sub__(self, other):
        return self.runtime.sub(self, other)

    def __rsub__(self, other):
        return self.runtime.sub(other, self)

    def __mul__(self, other):
        return self.runtime.mul(self, other)

    def __rmul__(self, other):
        return self.runtime.mul(other, self)

    # TODO: The xor implementation below does not work, and it is not
    # really a good idea either. Instead there should be a single xor
    # method in Runtime or two classes of Shares. So keep using
    # xor_int and xor_bit for now.
    #
    #def __xor__(self, other):
    #    def run_correct_xor(shares):
    #        if isinstance(shares[0], GF256) or isinstance(shares[1], GF256):
    #            return self.runtime.xor_bit(shares[0], shares[1])
    #        else:
    #            return self.runtime.xor_int(shares[0], shares[1])
    #
    #    shares = gather_shares([self, other])
    #    shares.addCallback(run_correct_xor)
    #    return shares

    def clone(self):
        def split_result(result):
            clone.callback(result)
            return result
        clone = Share(self.runtime)
        self.addCallback(split_result)
        return clone


# Roughly based on defer.DeferredList
class ShareList(Share):
    def __init__(self, shares, threshold=None):
        assert len(shares) > 0, "Cannot create empty ShareList"
        assert threshold is None or 0 < threshold <= len(shares), \
            "Threshold out of range"

        Share.__init__(self, shares[0].runtime)

        self.results = [None] * len(shares)
        if threshold is None:
            self.missing_shares = len(shares)
        else:
            self.missing_shares = threshold

        for index, share in enumerate(shares):
            share.addCallbacks(self._callback_fired, self._callback_fired,
                               callbackArgs=(index, True),
                               errbackArgs=(index, False))

    def _callback_fired(self, result, index, success):
         self.results[index] = (success, result)
         self.missing_shares -= 1
         if not self.called and self.missing_shares == 0:
             self.callback(self.results)
         return result


# Roughly based on defer.gatherResults
def gather_shares(shares):
    def filter_results(results):
        return [share for (success, share) in results]
    share_list = ShareList(shares)
    share_list.addCallback(filter_results)
    return share_list


class ShareExchanger(Int16StringReceiver):
    """Send and receive shares.

    All players are connected by pair-wise connections and this
    Twisted protocol is one such connection. It is used to send and
    receive shares from one other player.

    The C{marshal} module is used for converting the data to bytes for
    the network and to convert back again to structured data.
    """

    def __init__(self, id, runtime):
        """Initialize the protocol.

        @param id: the id of the peer.
        """
        self.id = id
        self.runtime = runtime
        
    def stringReceived(self, string):
        """Called when a share is received.

        The string received is unmarshalled into the program counter,
        the field modulus and the field value. The field element is
        reconstructed and the appropriate Deferred in the
        L{ShareExchangerFactory.incoming_shares} is triggered.

        @param string: bytes from the network.
        @type string: C{(program_counter, modulus, value)} in
        marshalled form
        """
        program_counter, modulus, value = marshal.loads(string)

        share = GF(modulus)(value)
        key = (program_counter, self.id)

        shares = self.factory.incoming_shares
        try:
            reactor.callLater(0, shares.pop(key).callback, share)
        except KeyError:
            shares[key] = Share(self.runtime, share)

        # TODO: marshal.loads can raise EOFError, ValueError, and
        # TypeError. They should be handled somehow.

    def sendShare(self, program_counter, share):
        """Send a share.

        The program counter and the share are marshalled and sent to
        the peer.

        @param program_counter: the program counter associated with
        the share.

        @return: C{self} so that C{sendShare} can be used as a
        callback.
        """
        #println("Sending to id=%d: program_counter=%s, share=%s",
        #        self.id, program_counter, share)

        data = (program_counter, share.modulus, share.value)
        self.sendString(marshal.dumps(data))
        return self

    def loseConnection(self):
        """Disconnect this protocol instance."""
        self.transport.loseConnection()
        # TODO: this ought to be the last callback and so it might not
        # be necessary to pass self on?
        return self

class ShareExchangerFactory(ServerFactory, ClientFactory):
    """Factory for creating ShareExchanger protocols."""

    def __init__(self, runtime, incoming_shares, port_player_mapping, protocols):
        """Initialize the factory.

        @param incoming_shares: incoming shares waiting for network
        input, see L{Runtime.incoming_shares}.
        @type incoming_shares: mapping from C{(program_counter, id)}
        pairs to Deferreds yielding shares
        """
        println("ShareExchangerFactory: %s", port_player_mapping)

        self.runtime = runtime

        #: Incoming shares. Reference to L{Runtime.incoming_shares}.
        self.incoming_shares = incoming_shares

        #: Mapping from C{(ip, port)} tuples to player IDs. Used to
        #: identify peers when they connect.
        self.port_player_mapping = port_player_mapping

        #: List of deferred protocols. These deferreds will be
        #: triggered when peers connect.
        self.protocols = protocols

    def buildProtocol(self, addr):
        """Build and return a new protocol for communicating with addr."""
        port = addr.port - (addr.port % 100)
        # Resolving the hostname into an IP address is a blocking
        # operation, but this is acceptable since buildProtocol is
        # only called when the runtime is initialized.
        ip = socket.gethostbyname(addr.host)
        id = self.port_player_mapping[(ip, port)]
        println("Peer id: %s", id)
        
        p = ShareExchanger(id, self.runtime)
        p.factory = self

        reactor.callLater(0, self.protocols[id].callback, p)
        return p

 
def increment_pc(method):
    """Make method automatically increment the program counter.

    Adding this decorator to a L{Runtime} method will ensure that the
    program counter is incremented correctly when entering the method.

    @param method: the method.
    @type method: a method of L{Runtime}
    """
    def inc_pc_wrapper(self, *args, **kwargs):
        try:
            self.program_counter[-1] += 1
            self.program_counter.append(0)
            #println("Calling %s: %s", method.func_name, self.program_counter)
            return method(self, *args, **kwargs)
        finally:
            self.program_counter.pop()
    inc_pc_wrapper.func_name = method.func_name
    return inc_pc_wrapper


class Runtime:
    """The VIFF runtime.

    Each party in the protocol must instantiate an object from this
    class and use it for all calculations.
    """

    def __init__(self, players, id, threshold):
        """Initialize runtime.

        The runtime is initialized based on the player configuration
        given, the id, and the threshold.

        @param players: player configuration, see L{config.load_config}.
        @type players: mapping from id to L{Player} instances
        @param id: id of this player.
        @param threshold: threshold for the protocol run.
        """
        #: Player configuration.
        self.players = players
        #: ID of this player.
        self.id = id
        #: Shamir secret sharing threshold.
        self.threshold = threshold

        #: Current program counter.
        #:
        #: Whenever a share is sent over the network, it must be
        #: uniquely identified so that the receiving player known what
        #: operation the share is a result of. This is done by
        #: associating a X{program counter} with each operation.
        #:
        #: Keeping the program counter synchronized between all
        #: players ought to be easy, but because of the asynchronous
        #: nature of network protocols, all players might not reach
        #: the same parts of the program at the same time.
        #:
        #: Consider two players M{A} and M{B} who are both waiting on
        #: the variables C{a} and C{b}. Callbacks have been added to
        #: C{a} and C{b}, and the question is what program counter the
        #: callbacks should use when sending data out over the
        #: network.
        #:
        #: Let M{A} receive input for C{a} and then for C{b} a little
        #: later, and let M{B} receive the inputs in reversed order so
        #: that the input for C{b} arrives first. The goal is to keep
        #: the program counters synchronized so that program counter
        #: M{x} refers to the same operation on all players. Because
        #: the inputs arrive in different order at different players,
        #: incrementing a simple global counter is not enough.
        #:
        #: Instead, a I{tree} is made, which follows the tree of
        #: execution. At the top level the program counter starts at
        #: C{[0]}. At the next operation it becomes C{[1]}, and so on.
        #: If a callback is scheduled (see L{callback}) at program
        #: counter C{[x, y, z]}, any calls it makes will be numbered
        #: C{[x, y, z, 1]}, then C{[x, y, z, 2]}, and so on.
        #:
        #: Maintaining such a tree of program counters ensures that
        #: different parts of the program execution never reuses the
        #: same program counter for different variables.
        #:
        #: The L{increment_pc} decorator is responsible for
        #: dynamically building the tree as the execution unfolds and
        #: L{callback} is responsible for scheduling callbacks with
        #: the correct program counter.
        #:
        #: @type: C{list} of integers.
        self.program_counter = [0]

        #: Future shares still waiting on input.
        #:
        #: Shares from other players are put here, either as an empty
        #: Deferred if we are waiting on input from the player, or as
        #: a succeeded Deferred if input is received from the other
        #: player before we are ready to use it.
        #:
        #: When we have a share to exchange with another player,
        #: L{_exchange_shares} is used. If we are ahead of the other
        #: player, it sets up a Deferred waiting for the players
        #: input. It is L{ShareExchanger.stringReceived} that triggers
        #: this deferred when the input eventually arrives. If the
        #: other player has already sent us its input, then
        #: L{ShareExchanger.stringReceived} has set up a succeeded
        #: Deferred which L{_exchange_shares} can simply return.
        #:
        #: @type: C{dict} from C{(program_counter, player_id)} to
        #: deferred shares.
        self.incoming_shares = {}

        #: Connections to the other players.
        #:
        #: These Deferreds will yield L{ShareExchanger} objects when
        #: other players make connections to us, and when we
        #: L{connect} to them.
        #:
        #: @type: C{list} of deferred L{ShareExchanger} objects.
        self.protocols = dict((id, Deferred())
                              for id, p in players.iteritems())
        self.protocols[self.id].callback("Unused")
        # Now try connecting...
        self.connect()

    def connect(self):
        """Connects this runtime to the others.

        Each player has pair-wise connections to all other players.
        With M{n} players a total of M{n * (n-1)/2 = (n^2-n)/2}
        connections are made.

        When the connections are made, the deferred L{ShareExchanger}
        objects in L{Runtime.protocols} are triggered.
        """
        # Resolving the hostname into an IP address is a blocking
        # operation, but this is acceptable since it is only done when
        # the runtime is initialized.
        ip_addresses = [socket.gethostbyname(p.host)
                        for p in self.players.values()]

        mapping = dict()
        for ip, (id, p) in zip(ip_addresses, self.players.iteritems()):
            mapping[(ip, p.port)] = id

        factory = ShareExchangerFactory(self, self.incoming_shares,
                                        mapping, self.protocols)
        reactor.listenTCP(self.players[self.id].port, factory)

        for id, player in self.players.iteritems():
            if id > self.id:
                bind_port = self.players[self.id].port + id
                println("Will connect to %s from port %d", player, bind_port)
                reactor.connectTCP(player.host, player.port, factory,
                                   bindAddress=(self.players[self.id].host,
                                                bind_port))

        println("Initialized Runtime with %d players, threshold %d",
                len(self.players), self.threshold)

    def shutdown(self):
        """Shutdown the runtime.

        All connections are closed and the runtime cannot be used
        again after this has been called.
        """
        println("Initiating shutdown sequence.")
        for protocol in self.protocols.itervalues():
            protocol.addCallback(lambda p: p.loseConnection())
        println("Waiting 1 second")
        reactor.callLater(1, reactor.stop)

    def wait_for(self, *vars):
        """Start the runtime and wait for the variables given.

        The runtime is shut down when all variables are calculated.

        @param vars: variables to wait for.
        @type  vars: list of L{Deferred}s
        """
        dl = DeferredList(vars)
        dl.addCallback(lambda _: self.shutdown())
        reactor.run()
        println("Reactor stopped")

    def callback(self, deferred, func, *args, **kwargs):
        """Schedule a callback on a deferred with the correct program
        counter.

        If a callback depends on the current program counter, then use
        this method to schedule it instead of simply calling
        addCallback directly. Simple callbacks that are independent of
        the program counter can still be added directly to the
        Deferred as usual.

        Any extra arguments are passed to the callback as with
        addCallback.

        @param deferred: the Deferred.
        @param func: the callback.
        @param args: extra arguments.
        @param kwargs: extra keyword arguments.
        """
        saved_pc = self.program_counter[:]
        #println("Saved PC: %s for %s", saved_pc, func.func_name)

        def callback_wrapper(*args, **kwargs):
            """Wrapper for a callback which ensures a correct PC."""
            try:
                current_pc = self.program_counter
                self.program_counter = saved_pc
                #println("Callback PC: %s", self.program_counter)
                return func(*args, **kwargs)
            finally:
                self.program_counter = current_pc
        callback_wrapper.func_name = func.func_name

        #println("Adding %s to %s", func.func_name, deferred)
        deferred.addCallback(callback_wrapper, *args, **kwargs)

    @increment_pc
    def open(self, sharing, threshold=None):
        """Open a share using the threshold given or the runtime
        default if threshold is None. Returns a new Deferred which
        will contain the opened value.

        Communication cost: 1 broadcast (for each player, n broadcasts
        in total).
        """
        assert isinstance(sharing, Share)
        if threshold is None:
            threshold = self.threshold

        def broadcast(share):
            """Broadcast share to all players."""
            assert isinstance(share, FieldElement)
            deferreds = []
            for id in self.players:
                d = self._exchange_shares(id, share)
                self.callback(d, lambda s, id: (s.field(id), s), id)
                deferreds.append(d)

            # TODO: This list ought to trigger as soon as more than
            # threshold shares has been received.
            return self._recombine(deferreds, threshold)

        result = sharing.clone()
        self.callback(result, broadcast)
        return result
        
    def add(self, share_a, share_b):
        """Addition of shares.

        Communication cost: none.
        """
        if not isinstance(share_a, Share):
            share_a = Share(self, share_a)
        if not isinstance(share_b, Share):
            share_b = Share(self, share_b)

        result = gather_shares([share_a, share_b])
        result.addCallback(lambda (a, b): a + b)
        return result

    def sub(self, share_a, share_b):
        """Subtraction of shares.

        Communication cost: none.
        """
        if not isinstance(share_a, Share):
            share_a = Share(self, share_a)
        if not isinstance(share_b, Share):
            share_b = Share(self, share_b)

        result = gather_shares([share_a, share_b])
        result.addCallback(lambda (a, b): a - b)
        return result

    @increment_pc
    def mul(self, share_a, share_b):
        """Multiplication of shares.

        Communication cost: 1 Shamir sharing.
        """
        # TODO:  mul accept FieldElements and do quick local
        # multiplication in that case. If two FieldElements are given,
        # return a FieldElement.

        if not isinstance(share_a, Share):
            share_a = Share(self, share_a)
        if not isinstance(share_b, Share):
            share_b = Share(self, share_b)

        result = gather_shares([share_a, share_b])
        result.addCallback(lambda (a, b): a * b)
        self.callback(result, self._shamir_share)
        self.callback(result, self._recombine, threshold=2*self.threshold)
        return result
    
    @increment_pc
    def xor_int(self, share_a, share_b):
        """Exclusive-or of integer sharings.
        
        Communication cost: 1 multiplication.
        """
        if not isinstance(share_a, Share):
            share_a = Share(self, share_a)
        if not isinstance(share_b, Share):
            share_b = Share(self, share_b)

        share_c = self.mul(share_a, share_b)
        result = gather_shares([share_a, share_b, share_c])
        self.callback(result, lambda (a, b, c): a + b - 2*c)
        return result

    #: Exclusive-or of GF256 sharings.
    #:
    #: Communication cost: none.
    xor_bit = add

    @increment_pc
    def prss_share(self, element):
        """PRSS share a field element.

        Communication cost: 1 broadcast.
        """
        assert isinstance(element, FieldElement)
        field = type(element)
        n = len(self.players)

        # Key used for PRSS.
        key = tuple(self.program_counter)

        # The shares for which we have all the keys.
        all_shares = []
        
        # Shares we calculate from doing PRSS with the other players.
        tmp_shares = {}

        prfs = self.players[self.id].dealer_prfs(field.modulus)

        # TODO: Stop calculating shares for all_shares at
        # self.threshold+1 since that is all we need to do the Shamir
        # recombine below.
        for player in self.players:
            # TODO: when player == self.id, the two calls to prss are
            # the same.
            share = prss(n, player, field, prfs[self.id], key)
            all_shares.append((field(player), share))
            tmp_shares[player] = prss(n, self.id, field, prfs[player], key)

        # We can now calculate what was shared and derive the
        # correction factor.
        shared = shamir.recombine(all_shares[:self.threshold+1])
        correction = element - shared

        result = []
        for player in self.players:
            # TODO: more efficient broadcast?
            d = self._exchange_shares(player, correction)
            # We have to add our own share to the correction factor
            # received.
            d.addCallback(lambda c, s: s + c, tmp_shares[player])
            result.append(d)
        return result

    @increment_pc
    def prss_share_random(self, field, binary=False):
        """Generate shares of a uniformly random element from the field given.

        If binary is True, a 0/1 element is generated. No player
        learns the value of the element.

        Communication cost: none if binary=False, 1 open otherwise.
        """
        if field is GF256 and binary:
            modulus = 2
        else:
            modulus = field.modulus

        # Key used for PRSS.
        prss_key = tuple(self.program_counter)
        prfs = self.players[self.id].prfs(modulus)
        share = prss(len(self.players), self.id, field, prfs, prss_key)

        if field is GF256 or not binary:
            return Share(self, share)

        # Open the square and compute a square-root
        result = self.open(self.mul(share, share), 2*self.threshold)

        def finish(square, share, binary):
            if square == 0:
                # We were unlucky, try again...
                return self.prss_share_random(field, binary)
            else:
                # We can finish the calculation
                root = square.sqrt()
                # When the root is computed, we divide the share and
                # convert the resulting -1/1 share into a 0/1 share.
                two = field(2)
                return Share(self, (share/root + 1) / two)

        self.callback(result, finish, share, binary)
        return result

    @increment_pc
    def _shamir_share(self, number):
        """Share a FieldElement using Shamir sharing.

        Returns a list of (id, share) pairs.
        """
        shares = shamir.share(number, self.threshold, len(self.players))
        #println("Shares of %s: %s", number, shares)
        
        result = []
        for other_id, share in shares:
            d = self._exchange_shares(other_id.value, share)
            d.addCallback(lambda share, id: (id, share), other_id)
            result.append(d)

        return result

    @increment_pc
    def shamir_share(self, number):
        """Share a field element using Shamir sharing.

        Returns a list of shares.

        Communication cost: n elements transmitted.
        """
        assert isinstance(number, FieldElement)

        def split(pair):
            pair.addCallback(lambda (_, share): share)

        result = self._shamir_share(number)
        map(split, result)
        return result

    @increment_pc
    def convert_bit_share(self, share, src_field, dst_field):
        """Convert a 0/1 share from src_field into dst_field."""
        bit = rand.randint(0, 1)
        dst_shares = self.prss_share(dst_field(bit))
        src_shares = self.prss_share(src_field(bit))

        # TODO: merge xor_int and xor_bit into an xor method and move
        # this decision there.
        if src_field is GF256:
            xor = self.xor_bit
        else:
            xor = self.xor_int
        
        # TODO: Using a parallel reduce below seems to be slower than
        # using the built-in reduce.

        # We open tmp and convert the value into a field element from
        # the dst_field.
        tmp = self.open(reduce(xor, src_shares, share))
        tmp.addCallback(lambda i: dst_field(i.value))
        
        if dst_field is GF256:
            xor = self.xor_bit
        else:
            xor = self.xor_int

        return reduce(xor, dst_shares, tmp)

    @increment_pc
    def convert_bit_share_II(self, share, src_field, dst_field, k=None):
        """Convert a 0/1 share from src_field into dst_field."""

        #TODO: don't do log like this...
        def log(x):
            result = 0
            while x > 1:
                result += 1
                x /= 2
            return result+1 # Error for powers of two...

        if k is None:
            k = 30
        l = k + log(dst_field.modulus)
        # TODO assert field sizes are OK...

        this_mask = rand.randint(0, (2**l) -1)

        # Share large random values in the big field and reduced ones
        # in the small...
        src_shares = self.prss_share(src_field(this_mask))
        dst_shares = self.prss_share(dst_field(this_mask))

        tmp = reduce(self.add, src_shares, share)

        # We open tmp and convert the value into a field element from
        # the dst_field.
        tmp = self.open(tmp)

        tmp.addCallback(lambda i: dst_field(i.value))

        full_mask = reduce(self.add, dst_shares)

        return self.sub(tmp, full_mask)

    @increment_pc
    def greater_than(self, share_a, share_b, field):
        """Compute share_a >= share_b.

        Both arguments must be from the field given. The result is a
        GF256 share.
        """
        # TODO: get these from a configuration file or similar
        l = 32 # bit-length of input numbers
        m = l + 2
        t = m + 1

        # Preprocessing begin

        assert 2**(l+1) + 2**t < field.modulus, "2^(l+1) + 2^t < p must hold"
        assert len(self.players) + 2 < 2**l

        int_bits = [self.prss_share_random(field, True) for _ in range(m)]
        # We must use int_bits without adding callbacks to the bits --
        # having int_b wait on them ensures this.

        def bits_to_int(bits):
            """Converts a list of bits to an integer."""
            return sum([2**i * b for i, b in enumerate(bits)])

        int_b = gather_shares(int_bits)
        int_b.addCallback(bits_to_int)

        # TODO: this changes int_bits! It should be okay since
        # int_bits is not used any further, but still...
        bit_bits = [self.convert_bit_share(b, field, GF256) for b in int_bits]
        # Preprocessing done

        a = self.add(self.sub(share_a, share_b), 2**l)
        T = self.open(self.add(self.sub(2**t, int_b), a))

        result = gather_shares([T] + bit_bits)
        self.callback(result, self._finish_greater_than, l)
        return result

    @increment_pc
    def _finish_greater_than(self, results, l):
        """Finish the calculation."""
        T = results[0]
        bit_bits = results[1:]

        vec = [(GF256(0), GF256(0))]

        # Calculate the vector, using only the first l bits
        for i, bi in enumerate(bit_bits[:l]):
            Ti = GF256(T.bit(i))
            ci = self.xor_bit(bi, Ti)
            vec.append((ci, Ti))

        # Reduce using the diamond operator. We want to do as much
        # as possible in parallel while being careful not to
        # switch the order of elements since the diamond operator
        # is non-commutative.
        while len(vec) > 1:
            tmp = []
            while len(vec) > 1:
                tmp.append(self._diamond(vec.pop(0), vec.pop(0)))
            if len(vec) == 1:
                tmp.append(vec[0])
            vec = tmp

        return self.xor_bit(GF256(T.bit(l)),
                            self.xor_bit(bit_bits[l], vec[0][1]))

    @increment_pc
    def _diamond(self, (top_a, bot_a), (top_b, bot_b)):
        """The "diamond-operator".

        Defined by

        (x, X) `diamond` (0, Y) = (0, Y)
        (x, X) `diamond` (1, Y) = (x, X)
        """
        top = self.mul(top_a, top_b)
        #   = x * y
        bot = self.xor_bit(self.mul(top_b, self.xor_bit(bot_a, bot_b)), bot_b)
        #   = (y * (X ^ Y)) ^ Y
        return (top, bot)

    ########################################################################
    ########################################################################

    @increment_pc
    def greater_thanII_preproc(self, field, smallField=None, l=None, k=None):
        """Preprocessing for greater_thanII."""
        if smallField is None:
            smallField = field
        if l is None:
            l = 32
        if k is None:
            k = 30
        # l++ for technical reasons
        # need an extra bit to avoid troubles with equal inputs
        l += 1

        # TODO: verify asserts are correct...
        assert field.modulus > 2**(l+2) + 2**(l+k), "Field too small"
        assert smallField.modulus > 3 + 3*l, "smallField too small"

        # TODO: do not generate all bits, only $l$ of them
        # could perhaps do PRSS over smaller subset?
        r_bitsField = [self.prss_share_random(field, True) for _ in range(l+k)]

        # TODO: compute r_full from r_modl and top bits, not from scratch
        r_full = field(0)
        for i, b in enumerate(r_bitsField):
            r_full = self.add(r_full, self.mul(b, field(2**i)))

        r_bitsField = r_bitsField[:l]
        r_modl = field(0)
        for i, b in enumerate(r_bitsField):
            r_modl = self.add(r_modl, self.mul(b, field(2**i)))

        # Transfer bits to smallField
        if field is smallField:
            r_bits = r_bitsField
        else:
            r_bits = [self.convert_bit_share_II(bit, field, smallField) \
                      for bit in r_bitsField]

        s_bit = self.prss_share_random(field, binary=True)

        s_bitSmallField = self.convert_bit_share_II(s_bit, field, smallField)
        s_sign = self.add(smallField(1),
                          self.mul(s_bitSmallField, smallField(-2)))

        # m: uniformly random -- should be non-zero, however, this
        # happens with negligible probability
        # TODO: small field, no longer negligible probability of zero -- update
        mask = self.prss_share_random(smallField, False)
        mask_2 = self.prss_share_random(smallField, False)
        mask_OK = self.open(self.mul(mask, mask_2))
        #dprint("Mask_OK: %s", mask_OK)
        return field, smallField, s_bit, s_sign, mask, r_full, r_modl, r_bits

        ##################################################
        # Preprocessing done
        ##################################################
        

    @increment_pc
    def greater_thanII_online(self, share_a, share_b, preproc, field, l=None):
        """Compute share_a >= share_b.
        Result is shared.
        """
        if l == None:
            l = 32

        # increment l as a, b are increased
        l += 1
        # a = 2a+1; b= 2b // ensures inputs not equal
        share_a = self.add(self.mul(field(2), share_a), field(1))
        share_b = self.mul(field(2), share_b)
        
        ##################################################
        # Unpack preprocessing
        ##################################################
        #TODO: assert fields are the same...
        field, smallField, s_bit, s_sign, mask, r_full, r_modl, r_bits = preproc
        assert l == len(r_bits), "preprocessing does not match " \
            "online parameters"

        ##################################################
        # Begin online computation
        ##################################################
        # c = 2**l + a - b + r
        z = self.add(self.sub(share_a, share_b), field(2**l))
        c = self.open(self.add(r_full, z))

        self.callback(c, self._finish_greater_thanII,
                      l, field, smallField, s_bit, s_sign, mask, r_full,
                      r_modl, r_bits, z)
        return c

    @increment_pc
    def _finish_greater_thanII(self, c, l, field, smallField, s_bit, s_sign,
                               mask, r_full, r_modl, r_bits, z):
        """Finish the calculation."""
        c_bits = [smallField(c.bit(i)) for i in range(l)]

        sumXORs = [0]*l
        # sumXORs[i] = sumXORs[i+1] + r_bits[i+1] + c_(i+1)
        #                           - 2*r_bits[i+1]*c_(i+1)
        for i in range(l-2, -1, -1):
            # sumXORs[i] = \sum_{j=i+1}^{l-1} r_j\oplus c_j
            sumXORs[i] = self.add(sumXORs[i+1],
                                  self.xor_int(r_bits[i+1], c_bits[i+1]))
        E_tilde = []
        for i in range(len(r_bits)):
            ## s + rBit[i] - cBit[i] + 3 * sumXors[i];
            e_i = self.add(s_sign, self.sub(r_bits[i], c_bits[i]))
            e_i = self.add(e_i, self.mul(smallField(3), sumXORs[i]))
            E_tilde.append(e_i)
        E_tilde.append(mask) # Hack: will mult e_i and mask...

        while len(E_tilde) > 1:
            # TODO: pop() ought to be preferred? No: it takes the
            # just appended and thus works linearly... try with
            # two lists instead, pop(0) is quadratic if it moves
            # elements.
            E_tilde.append(self.mul(E_tilde.pop(0),
                                    E_tilde.pop(0)))

        E_tilde[0] = self.open(E_tilde[0])
        E_tilde[0].addCallback(lambda bit: field(bit.value != 0))
        non_zero = E_tilde[0]

        # UF == underflow
        UF = self.xor_int(non_zero, s_bit)

        # conclude the computation -- compute final bit and map to 0/1
        # return  2^(-l) * (z - (c%2**l - r%2**l + UF*2**l))
        #
        c_mod2l = field(c.value % 2**l)
        result = self.add(self.sub(c_mod2l, r_modl),
                          self.mul(UF, field(2**l)))
        result = self.sub(z, result)
        result = self.mul(result, ~(field(2**l)))
        return result
    # END _finish_greater_thanII
    
    @increment_pc
    def greater_thanII(self, share_a, share_b, field, l=None):
        """Compute share_a >= share_b.

        Both arguments must be of type field. The result is a
        field share.
        """
        # TODO: get these from a configuration file or similar
        k = 30 # security parameter
        if l is None:
            l = 32 # bit-length of input numbers

        preproc = self.greater_thanII_preproc(field, l=l, k=k)
        return self.greater_thanII_online(share_a, share_b, preproc, field, l=l)

    ########################################################################
    ########################################################################

    def _exchange_shares(self, id, share):
        """Exchange shares with another player.

        We send the player our share and record a Deferred which will
        trigger when the share from the other side arrives.
        """
        assert isinstance(share, FieldElement)
        #println("exchange_shares sending: program_counter=%s, id=%d, share=%s",
        #        self.program_counter, id, share)

        if id == self.id:
            return Share(self, share)
        else:
            # Convert self.program_counter to a hashable value in order
            # to use it as a key in self.incoming_shares.
            pc = tuple(self.program_counter)
            key = (pc, id)
            if key not in self.incoming_shares:
                self.incoming_shares[key] = Share(self)

            # Send the share to the other side
            self.protocols[id].addCallback(ShareExchanger.sendShare, pc, share)
            return self.incoming_shares[key]

    @increment_pc
    def _recombine(self, shares, threshold):
        """Shamir recombine a list of deferred (id,share) pairs."""
        assert len(shares) > threshold
        result = gatherResults(shares[:threshold+1])
        result.addCallback(shamir.recombine)
        return result
