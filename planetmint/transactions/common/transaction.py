# Copyright © 2020 Interplanetary Database Association e.V.,
# Planetmint and IPDB software contributors.
# SPDX-License-Identifier: (Apache-2.0 AND CC-BY-4.0)
# Code is Apache-2.0 and docs are CC-BY-4.0

"""Transaction related models to parse and construct transaction
payloads.

Attributes:
    UnspentOutput (namedtuple): Object holding the information
        representing an unspent output.

"""
from collections import namedtuple
from copy import deepcopy
from functools import lru_cache
import rapidjson

import base58
from cryptoconditions import Fulfillment, ThresholdSha256, Ed25519Sha256
from cryptoconditions.exceptions import (
    ParsingError, ASN1DecodeError, ASN1EncodeError)
try:
    from hashlib import sha3_256
except ImportError:
    from sha3 import sha3_256

from planetmint.transactions.common.crypto import PrivateKey, hash_data
from planetmint.transactions.common.exceptions import (
    KeypairMismatchException, InputDoesNotExist, DoubleSpend,
    InvalidHash, InvalidSignature, AmountError, AssetIdMismatch)
from planetmint.transactions.common.utils import serialize
from .memoize import memoize_from_dict, memoize_to_dict
from .input import Input
from .output import Output
from .transaction_link import TransactionLink

UnspentOutput = namedtuple(
    'UnspentOutput', (
        # TODO 'utxo_hash': sha3_256(f'{txid}{output_index}'.encode())
        # 'utxo_hash',   # noqa
        'transaction_id',
        'output_index',
        'amount',
        'asset_id',
        'condition_uri',
    )
)


class Transaction(object):
    """A Transaction is used to create and transfer assets.

        Note:
            For adding Inputs and Outputs, this class provides methods
            to do so.

        Attributes:
            operation (str): Defines the operation of the Transaction.
            inputs (:obj:`list` of :class:`~planetmint.transactions.common.
                transaction.Input`, optional): Define the assets to
                spend.
            outputs (:obj:`list` of :class:`~planetmint.transactions.common.
                transaction.Output`, optional): Define the assets to lock.
            asset (dict): Asset payload for this Transaction. ``CREATE``
                Transactions require a dict with a ``data``
                property while ``TRANSFER`` Transactions require a dict with a
                ``id`` property.
            metadata (dict):
                Metadata to be stored along with the Transaction.
            version (string): Defines the version number of a Transaction.
    """

    CREATE = 'CREATE'
    TRANSFER = 'TRANSFER'
    ALLOWED_OPERATIONS = (CREATE, TRANSFER)
    VERSION = '2.0'

    def __init__(self, operation, asset, inputs=None, outputs=None,
                 metadata=None, version=None, hash_id=None, tx_dict=None):
        """The constructor allows to create a customizable Transaction.

            Note:
                When no `version` is provided, one is being
                generated by this method.

            Args:
                operation (str): Defines the operation of the Transaction.
                asset (dict): Asset payload for this Transaction.
                inputs (:obj:`list` of :class:`~planetmint.transactions.common.
                    transaction.Input`, optional): Define the assets to
                outputs (:obj:`list` of :class:`~planetmint.transactions.common.
                    transaction.Output`, optional): Define the assets to
                    lock.
                metadata (dict): Metadata to be stored along with the
                    Transaction.
                version (string): Defines the version number of a Transaction.
                hash_id (string): Hash id of the transaction.
        """
        if operation not in self.ALLOWED_OPERATIONS:
            allowed_ops = ', '.join(self.__class__.ALLOWED_OPERATIONS)
            raise ValueError('`operation` must be one of {}'
                             .format(allowed_ops))

        # Asset payloads for 'CREATE' operations must be None or
        # dicts holding a `data` property. Asset payloads for 'TRANSFER'
        # operations must be dicts holding an `id` property.
        if (operation == self.CREATE and
                asset is not None and not (isinstance(asset, dict) and 'data' in asset)):
            raise TypeError(('`asset` must be None or a dict holding a `data` '
                             " property instance for '{}' Transactions".format(operation)))
        elif (operation == self.TRANSFER and
                not (isinstance(asset, dict) and 'id' in asset)):
            raise TypeError(('`asset` must be a dict holding an `id` property '
                             'for \'TRANSFER\' Transactions'))

        if outputs and not isinstance(outputs, list):
            raise TypeError('`outputs` must be a list instance or None')

        if inputs and not isinstance(inputs, list):
            raise TypeError('`inputs` must be a list instance or None')

        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError('`metadata` must be a dict or None')

        self.version = version if version is not None else self.VERSION
        self.operation = operation
        self.asset = asset
        self.inputs = inputs or []
        self.outputs = outputs or []
        self.metadata = metadata
        self._id = hash_id
        self.tx_dict = tx_dict

    @property
    def unspent_outputs(self):
        """UnspentOutput: The outputs of this transaction, in a data
        structure containing relevant information for storing them in
        a UTXO set, and performing validation.
        """
        if self.operation == self.CREATE:
            self._asset_id = self._id
        elif self.operation == self.TRANSFER:
            self._asset_id = self.asset['id']
        return (UnspentOutput(
            transaction_id=self._id,
            output_index=output_index,
            amount=output.amount,
            asset_id=self._asset_id,
            condition_uri=output.fulfillment.condition_uri,
        ) for output_index, output in enumerate(self.outputs))

    @property
    def spent_outputs(self):
        """Tuple of :obj:`dict`: Inputs of this transaction. Each input
        is represented as a dictionary containing a transaction id and
        output index.
        """
        return (
            input_.fulfills.to_dict()
            for input_ in self.inputs if input_.fulfills
        )

    @property
    def serialized(self):
        return Transaction._to_str(self.to_dict())

    def _hash(self):
        self._id = hash_data(self.serialized)

    def __eq__(self, other):
        try:
            other = other.to_dict()
        except AttributeError:
            return False
        return self.to_dict() == other

    def to_inputs(self, indices=None):
        """Converts a Transaction's outputs to spendable inputs.

            Note:
                Takes the Transaction's outputs and derives inputs
                from that can then be passed into `Transaction.transfer` as
                `inputs`.
                A list of integers can be passed to `indices` that
                defines which outputs should be returned as inputs.
                If no `indices` are passed (empty list or None) all
                outputs of the Transaction are returned.

            Args:
                indices (:obj:`list` of int): Defines which
                    outputs should be returned as inputs.

            Returns:
                :obj:`list` of :class:`~planetmint.transactions.common.transaction.
                    Input`
        """
        # NOTE: If no indices are passed, we just assume to take all outputs
        #       as inputs.
        indices = indices or range(len(self.outputs))
        return [
            Input(self.outputs[idx].fulfillment,
                  self.outputs[idx].public_keys,
                  TransactionLink(self.id, idx))
            for idx in indices
        ]

    def add_input(self, input_):
        """Adds an input to a Transaction's list of inputs.

            Args:
                input_ (:class:`~planetmint.transactions.common.transaction.
                    Input`): An Input to be added to the Transaction.
        """
        if not isinstance(input_, Input):
            raise TypeError('`input_` must be a Input instance')
        self.inputs.append(input_)

    def add_output(self, output):
        """Adds an output to a Transaction's list of outputs.

            Args:
                output (:class:`~planetmint.transactions.common.transaction.
                    Output`): An Output to be added to the
                    Transaction.
        """
        if not isinstance(output, Output):
            raise TypeError('`output` must be an Output instance or None')
        self.outputs.append(output)

    def sign(self, private_keys):
        """Fulfills a previous Transaction's Output by signing Inputs.

            Note:
                This method works only for the following Cryptoconditions
                currently:
                    - Ed25519Fulfillment
                    - ThresholdSha256
                Furthermore, note that all keys required to fully sign the
                Transaction have to be passed to this method. A subset of all
                will cause this method to fail.

            Args:
                private_keys (:obj:`list` of :obj:`str`): A complete list of
                    all private keys needed to sign all Fulfillments of this
                    Transaction.

            Returns:
                :class:`~planetmint.transactions.common.transaction.Transaction`
        """
        # TODO: Singing should be possible with at least one of all private
        #       keys supplied to this method.
        if private_keys is None or not isinstance(private_keys, list):
            raise TypeError('`private_keys` must be a list instance')

        # NOTE: Generate public keys from private keys and match them in a
        #       dictionary:
        #                   key:     public_key
        #                   value:   private_key
        def gen_public_key(private_key):
            # TODO FOR CC: Adjust interface so that this function becomes
            #              unnecessary

            # cc now provides a single method `encode` to return the key
            # in several different encodings.
            public_key = private_key.get_verifying_key().encode()
            # Returned values from cc are always bytestrings so here we need
            # to decode to convert the bytestring into a python str
            return public_key.decode()

        key_pairs = {gen_public_key(PrivateKey(private_key)):
                     PrivateKey(private_key) for private_key in private_keys}

        tx_dict = self.to_dict()
        tx_dict = Transaction._remove_signatures(tx_dict)
        tx_serialized = Transaction._to_str(tx_dict)
        for i, input_ in enumerate(self.inputs):
            self.inputs[i] = self._sign_input(input_, tx_serialized, key_pairs)

        self._hash()

        return self

    @classmethod
    def _sign_input(cls, input_, message, key_pairs):
        """Signs a single Input.

            Note:
                This method works only for the following Cryptoconditions
                currently:
                    - Ed25519Fulfillment
                    - ThresholdSha256.

            Args:
                input_ (:class:`~planetmint.transactions.common.transaction.
                    Input`) The Input to be signed.
                message (str): The message to be signed
                key_pairs (dict): The keys to sign the Transaction with.
        """
        if isinstance(input_.fulfillment, Ed25519Sha256):
            return cls._sign_simple_signature_fulfillment(input_, message,
                                                          key_pairs)
        elif isinstance(input_.fulfillment, ThresholdSha256):
            return cls._sign_threshold_signature_fulfillment(input_, message,
                                                             key_pairs)
        else:
            raise ValueError(
                'Fulfillment couldn\'t be matched to '
                'Cryptocondition fulfillment type.')

    @classmethod
    def _sign_simple_signature_fulfillment(cls, input_, message, key_pairs):
        """Signs a Ed25519Fulfillment.

            Args:
                input_ (:class:`~planetmint.transactions.common.transaction.
                    Input`) The input to be signed.
                message (str): The message to be signed
                key_pairs (dict): The keys to sign the Transaction with.
        """
        # NOTE: To eliminate the dangers of accidentally signing a condition by
        #       reference, we remove the reference of input_ here
        #       intentionally. If the user of this class knows how to use it,
        #       this should never happen, but then again, never say never.
        input_ = deepcopy(input_)
        public_key = input_.owners_before[0]
        message = sha3_256(message.encode())
        if input_.fulfills:
            message.update('{}{}'.format(
                input_.fulfills.txid, input_.fulfills.output).encode())

        try:
            # cryptoconditions makes no assumptions of the encoding of the
            # message to sign or verify. It only accepts bytestrings
            input_.fulfillment.sign(
                message.digest(), base58.b58decode(key_pairs[public_key].encode()))
        except KeyError:
            raise KeypairMismatchException('Public key {} is not a pair to '
                                           'any of the private keys'
                                           .format(public_key))
        return input_

    @classmethod
    def _sign_threshold_signature_fulfillment(cls, input_, message, key_pairs):
        """Signs a ThresholdSha256.

            Args:
                input_ (:class:`~planetmint.transactions.common.transaction.
                    Input`) The Input to be signed.
                message (str): The message to be signed
                key_pairs (dict): The keys to sign the Transaction with.
        """
        input_ = deepcopy(input_)
        message = sha3_256(message.encode())
        if input_.fulfills:
            message.update('{}{}'.format(
                input_.fulfills.txid, input_.fulfills.output).encode())

        for owner_before in set(input_.owners_before):
            # TODO: CC should throw a KeypairMismatchException, instead of
            #       our manual mapping here

            # TODO FOR CC: Naming wise this is not so smart,
            #              `get_subcondition` in fact doesn't return a
            #              condition but a fulfillment

            # TODO FOR CC: `get_subcondition` is singular. One would not
            #              expect to get a list back.
            ccffill = input_.fulfillment
            subffills = ccffill.get_subcondition_from_vk(
                base58.b58decode(owner_before))
            if not subffills:
                raise KeypairMismatchException('Public key {} cannot be found '
                                               'in the fulfillment'
                                               .format(owner_before))
            try:
                private_key = key_pairs[owner_before]
            except KeyError:
                raise KeypairMismatchException('Public key {} is not a pair '
                                               'to any of the private keys'
                                               .format(owner_before))

            # cryptoconditions makes no assumptions of the encoding of the
            # message to sign or verify. It only accepts bytestrings
            for subffill in subffills:
                subffill.sign(
                    message.digest(), base58.b58decode(private_key.encode()))
        return input_

    def inputs_valid(self, outputs=None):
        """Validates the Inputs in the Transaction against given
        Outputs.

            Note:
                Given a `CREATE` Transaction is passed,
                dummy values for Outputs are submitted for validation that
                evaluate parts of the validation-checks to `True`.

            Args:
                outputs (:obj:`list` of :class:`~planetmint.transactions.common.
                    transaction.Output`): A list of Outputs to check the
                    Inputs against.

            Returns:
                bool: If all Inputs are valid.
        """
        if self.operation == self.CREATE:
            # NOTE: Since in the case of a `CREATE`-transaction we do not have
            #       to check for outputs, we're just submitting dummy
            #       values to the actual method. This simplifies it's logic
            #       greatly, as we do not have to check against `None` values.
            return self._inputs_valid(['dummyvalue'
                                       for _ in self.inputs])
        elif self.operation == self.TRANSFER:
            return self._inputs_valid([output.fulfillment.condition_uri
                                       for output in outputs])
        else:
            allowed_ops = ', '.join(self.__class__.ALLOWED_OPERATIONS)
            raise TypeError('`operation` must be one of {}'
                            .format(allowed_ops))

    def _inputs_valid(self, output_condition_uris):
        """Validates an Input against a given set of Outputs.

            Note:
                The number of `output_condition_uris` must be equal to the
                number of Inputs a Transaction has.

            Args:
                output_condition_uris (:obj:`list` of :obj:`str`): A list of
                    Outputs to check the Inputs against.

            Returns:
                bool: If all Outputs are valid.
        """

        if len(self.inputs) != len(output_condition_uris):
            raise ValueError('Inputs and '
                             'output_condition_uris must have the same count')

        tx_dict = self.tx_dict if self.tx_dict else self.to_dict()
        tx_dict = Transaction._remove_signatures(tx_dict)
        tx_dict['id'] = None
        tx_serialized = Transaction._to_str(tx_dict)

        def validate(i, output_condition_uri=None):
            """Validate input against output condition URI"""
            return self._input_valid(self.inputs[i], self.operation,
                                     tx_serialized, output_condition_uri)

        return all(validate(i, cond)
                   for i, cond in enumerate(output_condition_uris))

    @lru_cache(maxsize=16384)
    def _input_valid(self, input_, operation, message, output_condition_uri=None):
        """Validates a single Input against a single Output.

            Note:
                In case of a `CREATE` Transaction, this method
                does not validate against `output_condition_uri`.

            Args:
                input_ (:class:`~planetmint.transactions.common.transaction.
                    Input`) The Input to be signed.
                operation (str): The type of Transaction.
                message (str): The fulfillment message.
                output_condition_uri (str, optional): An Output to check the
                    Input against.

            Returns:
                bool: If the Input is valid.
        """
        ccffill = input_.fulfillment
        try:
            parsed_ffill = Fulfillment.from_uri(ccffill.serialize_uri())
        except (TypeError, ValueError,
                ParsingError, ASN1DecodeError, ASN1EncodeError):
            return False

        if operation == self.CREATE:
            # NOTE: In the case of a `CREATE` transaction, the
            #       output is always valid.
            output_valid = True
        else:
            output_valid = output_condition_uri == ccffill.condition_uri

        message = sha3_256(message.encode())
        if input_.fulfills:
            message.update('{}{}'.format(
                input_.fulfills.txid, input_.fulfills.output).encode())

        # NOTE: We pass a timestamp to `.validate`, as in case of a timeout
        #       condition we'll have to validate against it

        # cryptoconditions makes no assumptions of the encoding of the
        # message to sign or verify. It only accepts bytestrings
        ffill_valid = parsed_ffill.validate(message=message.digest())
        return output_valid and ffill_valid

    # This function is required by `lru_cache` to create a key for memoization
    def __hash__(self):
        return hash(self.id)

    @memoize_to_dict
    def to_dict(self):
        """Transforms the object to a Python dictionary.

            Returns:
                dict: The Transaction as an alternative serialization format.
        """
        return {
            'inputs': [input_.to_dict() for input_ in self.inputs],
            'outputs': [output.to_dict() for output in self.outputs],
            'operation': str(self.operation),
            'metadata': self.metadata,
            'asset': self.asset,
            'version': self.version,
            'id': self._id,
        }

    @staticmethod
    # TODO: Remove `_dict` prefix of variable.
    def _remove_signatures(tx_dict):
        """Takes a Transaction dictionary and removes all signatures.

            Args:
                tx_dict (dict): The Transaction to remove all signatures from.

            Returns:
                dict

        """
        # NOTE: We remove the reference since we need `tx_dict` only for the
        #       transaction's hash
        tx_dict = deepcopy(tx_dict)
        for input_ in tx_dict['inputs']:
            # NOTE: Not all Cryptoconditions return a `signature` key (e.g.
            #       ThresholdSha256), so setting it to `None` in any
            #       case could yield incorrect signatures. This is why we only
            #       set it to `None` if it's set in the dict.
            input_['fulfillment'] = None
        return tx_dict

    @staticmethod
    def _to_hash(value):
        return hash_data(value)

    @property
    def id(self):
        return self._id

    def to_hash(self):
        return self.to_dict()['id']

    @staticmethod
    def _to_str(value):
        return serialize(value)

    # TODO: This method shouldn't call `_remove_signatures`
    def __str__(self):
        tx = Transaction._remove_signatures(self.to_dict())
        return Transaction._to_str(tx)

    @classmethod
    def get_asset_id(cls, transactions):
        """Get the asset id from a list of :class:`~.Transactions`.

        This is useful when we want to check if the multiple inputs of a
        transaction are related to the same asset id.

        Args:
            transactions (:obj:`list` of :class:`~planetmint.transactions.common.
                transaction.Transaction`): A list of Transactions.
                Usually input Transactions that should have a matching
                asset ID.

        Returns:
            str: ID of the asset.

        Raises:
            :exc:`AssetIdMismatch`: If the inputs are related to different
                assets.
        """

        if not isinstance(transactions, list):
            transactions = [transactions]

        # create a set of the transactions' asset ids
        asset_ids = {tx.id if tx.operation == tx.CREATE
                     else tx.asset['id']
                     for tx in transactions}

        # check that all the transasctions have the same asset id
        if len(asset_ids) > 1:
            raise AssetIdMismatch(('All inputs of all transactions passed'
                                   ' need to have the same asset id'))
        return asset_ids.pop()

    @staticmethod
    def validate_id(tx_body):
        """Validate the transaction ID of a transaction

            Args:
                tx_body (dict): The Transaction to be transformed.
        """
        # NOTE: Remove reference to avoid side effects
        # tx_body = deepcopy(tx_body)
        tx_body = rapidjson.loads(rapidjson.dumps(tx_body))

        try:
            proposed_tx_id = tx_body['id']
        except KeyError:
            raise InvalidHash('No transaction id found!')

        tx_body['id'] = None

        tx_body_serialized = Transaction._to_str(tx_body)
        valid_tx_id = Transaction._to_hash(tx_body_serialized)

        if proposed_tx_id != valid_tx_id:
            err_msg = ("The transaction's id '{}' isn't equal to "
                       "the hash of its body, i.e. it's not valid.")
            raise InvalidHash(err_msg.format(proposed_tx_id))

    @classmethod
    @memoize_from_dict
    def from_dict(cls, tx, skip_schema_validation=True):
        """Transforms a Python dictionary to a Transaction object.

            Args:
                tx_body (dict): The Transaction to be transformed.

            Returns:
                :class:`~planetmint.transactions.common.transaction.Transaction`
        """
        operation = tx.get('operation', Transaction.CREATE) if isinstance(tx, dict) else Transaction.CREATE
        cls = Transaction.resolve_class(operation)

        if not skip_schema_validation:
            cls.validate_id(tx)
            cls.validate_schema(tx)

        inputs = [Input.from_dict(input_) for input_ in tx['inputs']]
        outputs = [Output.from_dict(output) for output in tx['outputs']]
        return cls(tx['operation'], tx['asset'], inputs, outputs,
                   tx['metadata'], tx['version'], hash_id=tx['id'], tx_dict=tx)

    @classmethod
    def from_db(cls, planet, tx_dict_list):
        """Helper method that reconstructs a transaction dict that was returned
        from the database. It checks what asset_id to retrieve, retrieves the
        asset from the asset table and reconstructs the transaction.

        Args:
            planet (:class:`~planetmint.tendermint.Planetmint`): An instance
                of Planetmint used to perform database queries.
            tx_dict_list (:list:`dict` or :obj:`dict`): The transaction dict or
                list of transaction dict as returned from the database.

        Returns:
            :class:`~Transaction`

        """
        return_list = True
        if isinstance(tx_dict_list, dict):
            tx_dict_list = [tx_dict_list]
            return_list = False

        tx_map = {}
        tx_ids = []
        for tx in tx_dict_list:
            tx.update({'metadata': None})
            tx_map[tx['id']] = tx
            tx_ids.append(tx['id'])

        assets = list(planet.get_assets(tx_ids))
        for asset in assets:
            if asset is not None:
                tx = tx_map[asset['id']]
                del asset['id']
                tx['asset'] = asset

        tx_ids = list(tx_map.keys())
        metadata_list = list(planet.get_metadata(tx_ids))
        for metadata in metadata_list:
            tx = tx_map[metadata['id']]
            tx.update({'metadata': metadata.get('metadata')})

        if return_list:
            tx_list = []
            for tx_id, tx in tx_map.items():
                tx_list.append(cls.from_dict(tx))
            return tx_list
        else:
            tx = list(tx_map.values())[0]
            return cls.from_dict(tx)

    type_registry = {}

    @staticmethod
    def register_type(tx_type, tx_class):
        Transaction.type_registry[tx_type] = tx_class

    def resolve_class(operation):
        """For the given `tx` based on the `operation` key return its implementation class"""

        create_txn_class = Transaction.type_registry.get(Transaction.CREATE)
        return Transaction.type_registry.get(operation, create_txn_class)

    @classmethod
    def validate_schema(cls, tx):
        pass

    def validate_transfer_inputs(self, planet, current_transactions=[]):
        # store the inputs so that we can check if the asset ids match
        input_txs = []
        input_conditions = []
        for input_ in self.inputs:
            input_txid = input_.fulfills.txid
            input_tx = planet.get_transaction(input_txid)

            if input_tx is None:
                for ctxn in current_transactions:
                    if ctxn.id == input_txid:
                        input_tx = ctxn

            if input_tx is None:
                raise InputDoesNotExist("input `{}` doesn't exist"
                                        .format(input_txid))

            spent = planet.get_spent(input_txid, input_.fulfills.output,
                                     current_transactions)
            if spent:
                raise DoubleSpend('input `{}` was already spent'
                                  .format(input_txid))

            output = input_tx.outputs[input_.fulfills.output]
            input_conditions.append(output)
            input_txs.append(input_tx)

        # Validate that all inputs are distinct
        links = [i.fulfills.to_uri() for i in self.inputs]
        if len(links) != len(set(links)):
            raise DoubleSpend('tx "{}" spends inputs twice'.format(self.id))

        # validate asset id
        asset_id = self.get_asset_id(input_txs)
        if asset_id != self.asset['id']:
            raise AssetIdMismatch(('The asset id of the input does not'
                                   ' match the asset id of the'
                                   ' transaction'))

        input_amount = sum([input_condition.amount for input_condition in input_conditions])
        output_amount = sum([output_condition.amount for output_condition in self.outputs])

        if output_amount != input_amount:
            raise AmountError(('The amount used in the inputs `{}`'
                               ' needs to be same as the amount used'
                               ' in the outputs `{}`')
                              .format(input_amount, output_amount))

        if not self.inputs_valid(input_conditions):
            raise InvalidSignature('Transaction signature is invalid.')

        return True
