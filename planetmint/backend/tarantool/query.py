# Copyright © 2020 Interplanetary Database Association e.V.,
# Planetmint and IPDB software contributors.
# SPDX-License-Identifier: (Apache-2.0 AND CC-BY-4.0)
# Code is Apache-2.0 and docs are CC-BY-4.0

"""Query implementation for MongoDB"""

from pymongo import DESCENDING

from planetmint import backend
from planetmint.backend.exceptions import DuplicateKeyError
from planetmint.backend.utils import module_dispatch_registrar
from planetmint.backend.localmongodb.connection import LocalMongoDBConnection
from planetmint.common.transaction import Transaction

register_query = module_dispatch_registrar(backend.query)


def _group_transaction_by_ids(txids, connection):
    txspace = connection.space("transactions")
    inxspace = connection.space("inputs")
    outxspace = connection.space("outputs")
    keysxspace = connection.space("keys")
    _transactions = []
    for txid in txids:
        _txobject = txspace.select(txid, index="id_search")
        if len(_txobject.data) == 0:
            continue
        _txobject = _txobject.data[0]
        _txinputs = inxspace.select(txid, index="id_search")
        _txinputs = _txinputs.data
        _txoutputs = outxspace.select(txid, index="id_search")
        _txoutputs = _txoutputs.data
        _txkeys = keysxspace.select(txid, index="txid_search")
        _txkeys = _txkeys.data
        _obj = {
            "id": txid,
            "version": _txobject[2],
            "operation": _txobject[1],
            "inputs": [
                {
                    "owners_before": _in[2],
                    "fulfills": {"transaction_id": _in[3], "output_index": _in[4]},
                    "fulfillment": _in[1]
                } for _in in _txinputs
            ],
            "outputs": [
                {
                    "public_keys": [_key[2] for _key in _txkeys if _key[1] == _out[5]],
                    "amount": _out[1],
                    "condition": {"details": {"type": _out[3], "public_key": _out[4]}, "uri": _out[2]}
                } for _out in _txoutputs
            ]
        }
        if len(_txobject[3]) > 0:
            _obj["asset"] = {
                "id": _txobject[3]
            }
        _transactions.append(_obj)

    return _transactions


@register_query(LocalMongoDBConnection)
def store_transactions(signed_transactions: list,
                       connection):
    txspace = connection.space("transactions")
    inxspace = connection.space("inputs")
    outxspace = connection.space("outputs")
    keysxspace = connection.space("keys")
    for transaction in signed_transactions:
        txspace.insert((transaction["id"],
                        transaction["operation"],
                        transaction["version"],
                        transaction["asset"]["id"] if "asset" in transaction.keys() else ""
                        ))
        for _in in transaction["inputs"]:
            input_id = token_hex(7)
            inxspace.insert((transaction["id"],
                             _in["fulfillment"],
                             _in["owners_before"],
                             _in["fulfills"]["transaction_id"] if _in["fulfills"] is not None else "",
                             str(_in["fulfills"]["output_index"]) if _in["fulfills"] is not None else "",
                             input_id))
        for _out in transaction["outputs"]:
            output_id = token_hex(7)
            outxspace.insert((transaction["id"],
                              _out["amount"],
                              _out["condition"]["uri"],
                              _out["condition"]["details"]["type"],
                              _out["condition"]["details"]["public_key"],
                              output_id
                              ))
            for _key in _out["public_keys"]:
                keysxspace.insert((transaction["id"], output_id, _key))


@register_query(LocalMongoDBConnection)
def get_transaction(transaction_id: str, connection):
    _transactions = _group_transaction_by_ids(txids=[transaction_id], connection=connection)
    return next(iter(_transactions), None)


@register_query(LocalMongoDBConnection)
def get_transactions(transactions_ids: list, connection):
    _transactions = _group_transaction_by_ids(txids=transactions_ids, connection=connection)
    return _transactions


@register_query(LocalMongoDBConnection)
def store_metadatas(metadata, connection):
    space = connection.space("meta_data")
    for meta in metadata:
        space.insert((meta["id"], meta))


@register_query(LocalMongoDBConnection)
def get_metadata(transaction_ids: list, space):
    _returned_data = []
    for _id in transaction_ids:
        metadata = space.select(_id, index="id_search")
        _returned_data.append({"id": metadata.data[0][0], "metadata": metadata.data[0][1]})
    return _returned_data


@register_query(LocalMongoDBConnection)
def store_asset(asset, connection):
    space = connection.space("assets")
    unique = token_hex(8)
    space.insert((asset["id"], unique, asset["data"]))


@register_query(LocalMongoDBConnection)
def store_assets(assets, connection):
    space = connection.space("assets")
    for asset in assets:
        unique = token_hex(8)
        space.insert((asset["id"], unique, asset["data"]))


@register_query(LocalMongoDBConnection)
def get_asset(asset_id: str, space):
    _data = space.select(asset_id, index="assetid_search")
    _data = _data.data[0]
    return {"data": _data[1]}


@register_query(LocalMongoDBConnection)
def get_assets(assets_ids: list, space):
    _returned_data = []
    for _id in assets_ids:
        asset = space.select(_id, index="assetid_search")
        asset = asset.data[0]
        _returned_data.append({"id": asset[0], "data": asset[1]})
    return _returned_data


@register_query(LocalMongoDBConnection)
def get_spent(fullfil_transaction_id: str, fullfil_output_index: str, connection):
    _transaction_object = formats.transactions.copy()
    _transaction_object["inputs"] = []
    _transaction_object["outputs"] = []
    space = connection.space("inputs")
    _inputs = space.select([fullfil_transaction_id, fullfil_output_index], index="spent_search")
    _inputs = _inputs.data
    _transaction_object["id"] = _inputs[0][0]
    _transaction_object["inputs"] = [
        {
            "owners_before": _in[2],
            "fulfills": {"transaction_id": _in[3], "output_index": _in[4]},
            "fulfillment": _in[1]
        } for _in in _inputs
    ]
    space = connection.space("outputs")
    _outputs = space.select(_transaction_object["id"], index="id_search")
    _outputs = _outputs.data
    _transaction_object["outputs"] = [
        {
            "public_keys": _out[5],
            "amount": _out[1],
            "condition": {"details": {"type": _out[3], "public_key": _out[4]}, "uri": _out[2]}
        } for _out in _outputs
    ]
    return _transaction_object


@register_query(LocalMongoDBConnection)
def latest_block(connection):  # TODO Here is used DESCENDING OPERATOR
    space = connection.space("blocks")
    _all_blocks = space.select()
    _all_blocks = _all_blocks.data
    _block = sorted(_all_blocks, key=itemgetter(1))[0]
    space = connection.space("blocks_tx")
    _txids = space.select(_block[2], index="block_search")
    _txids = _txids.data
    return {"app_hash": _block[1], "height": _block[1], "transactions": [tx[0] for tx in _txids]}


@register_query(LocalMongoDBConnection)
def store_block(block, connection):
    space = connection.space("blocks")
    block_unique_id = token_hex(8)
    space.insert((block["app_hash"],
                  block["height"],
                  block_unique_id))
    space = connection.space("blocks_tx")
    for txid in block["transactions"]:
        space.insert((txid, block_unique_id))


@register_query(LocalMongoDBConnection)
def get_txids_filtered(connection, asset_id, operation=None, last_tx=None):  # TODO here is used 'OR' operator
    _transaction_object = formats.transactions.copy()
    _transaction_object["inputs"] = []
    _transaction_object["outputs"] = []

    actions = {
        "CREATE": {"sets": ["CREATE", asset_id], "index": "transaction_search"},
        # 1 - operation, 2 - id (only in transactions) +
        "TRANSFER": {"sets": ["TRANSFER", asset_id], "index": "asset_search"},
        # 1 - operation, 2 - asset.id (linked mode) + OPERATOR OR
        None: {"sets": [asset_id, asset_id], "index": "both_search"}
    }[operation]
    space = connection.space("transactions")
    if actions["sets"][0] == "CREATE":
        _transactions = space.select([operation, asset_id], index=actions["index"])
        _transactions = _transactions.data
    elif actions["sets"][0] == "TRANSFER":
        _transactions = space.select([operation, asset_id], index=actions["index"])
        _transactions = _transactions.data
    else:
        _transactions = space.select([asset_id, asset_id], index=actions["index"])
        _transactions = _transactions.data

    if last_tx:
        _transactions = [_transactions[0]]

    return tuple([elem[0] for elem in _transactions])


@register_query(LocalMongoDBConnection)
def text_search(conn, search, *, language='english', case_sensitive=False,
                # TODO review text search in tarantool (maybe, remove)
                diacritic_sensitive=False, text_score=False, limit=0, table='assets'):
    cursor = conn.run(
        conn.collection(table)
            .find({'$text': {
            '$search': search,
            '$language': language,
            '$caseSensitive': case_sensitive,
            '$diacriticSensitive': diacritic_sensitive}},
            {'score': {'$meta': 'textScore'}, '_id': False})
            .sort([('score', {'$meta': 'textScore'})])
            .limit(limit))

    if text_score:
        return cursor

    return (_remove_text_score(obj) for obj in cursor)


def _remove_text_score(asset):
    asset.pop('score', None)
    return asset


@register_query(LocalMongoDBConnection)
def get_owned_ids(connection, owner):  # TODO To make a test
    space = connection.space("keys")
    _keys = space.select(owner, index="keys_search", limit=1)
    if len(_keys.data) == 0:
        return []
    _transactionid = _keys[0][0]
    _transactions = _group_transaction_by_ids(txids=[_transactionid], connection=connection)
    return _transactions


@register_query(LocalMongoDBConnection)
def get_spending_transactions(inputs, connection):
    transaction_ids = [i['transaction_id'] for i in inputs]
    output_indexes = [i['output_index'] for i in inputs]

    _transactions = []

    for i in range(0, len(transaction_ids)):
        ts_id = transaction_ids[i]
        ot_id = output_indexes[i]

        _trans_object = get_spent(fullfil_transaction_id=ts_id, fullfil_output_index=ot_id, connection=connection)
        _transactions.append(_trans_object)

    return _transactions


@register_query(LocalMongoDBConnection)
def get_block(block_id, connection):
    space = connection.space("blocks")
    _block = space.select(block_id, index="block_search", limit=1)
    _block = _block.data[0]
    _txblock = space.select(_block[2], index="block_search")
    _txblock = _txblock.data
    return {"app_hash": _block[0], "height": _block[1], "transactions": [_tx[0] for _tx in _txblock]}


@register_query(LocalMongoDBConnection)
def get_block_with_transaction(txid, connection):
    space = connection.space("blocks_tx")
    _all_blocks_tx = space.select(txid, index="id_search")
    _all_blocks_tx = _all_blocks_tx.data
    space = connection.space("blocks")

    _block = space.select(_all_blocks_tx[0][1], index="block_id_search")
    _block = _block.data[0]
    return {"app_hash": _block[0], "height": _block[1], "transactions": [_tx[0] for _tx in _all_blocks_tx]}


@register_query(LocalMongoDBConnection)
def delete_transactions(connection, txn_ids):
    space = connection.space("transactions")
    for _id in txn_ids:
        space.delete(_id)
    inputs_space = connection.space("inputs")
    outputs_space = connection.space("outputs")
    for _id in txn_ids:
        _inputs = inputs_space.select(_id, index="id_search")
        _outputs = outputs_space.select(_id, index="id_search")
        for _inpID in _inputs:
            space.delete(_inpID[5])
        for _outpID in _outputs:
            space.delete(_outpID[5])


@register_query(LocalMongoDBConnection)
def store_unspent_outputs(conn, *unspent_outputs):
    if unspent_outputs:
        try:
            return conn.run(
                conn.collection('utxos').insert_many(
                    unspent_outputs,
                    ordered=False,
                )
            )
        except DuplicateKeyError:
            # TODO log warning at least
            pass


@register_query(LocalMongoDBConnection)
def delete_unspent_outputs(conn, *unspent_outputs):
    if unspent_outputs:
        return conn.run(
            conn.collection('utxos').delete_many({
                '$or': [{
                    '$and': [
                        {'transaction_id': unspent_output['transaction_id']},
                        {'output_index': unspent_output['output_index']},
                    ],
                } for unspent_output in unspent_outputs]
            })
        )


@register_query(LocalMongoDBConnection)
def get_unspent_outputs(conn, *, query=None):
    if query is None:
        query = {}
    return conn.run(conn.collection('utxos').find(query,
                                                  projection={'_id': False}))


@register_query(LocalMongoDBConnection)
def store_pre_commit_state(state, connection):
    space = connection.space("pre_commits")
    _precommit = space.select(state["height"], index="height_search", limit=1)
    unique_id = token_hex(8) if (len(_precommit.data) == 0) else _precommit.data[0][0]
    space.upsert((unique_id, state["height"], state["transactions"]),
                 op_list=[('=', 0, unique_id),
                          ('=', 1, state["height"]),
                          ('=', 2, state["transactions"])],
                 limit=1)


@register_query(LocalMongoDBConnection)
def get_pre_commit_state(conn):
    return conn.run(conn.collection('pre_commit').find_one())


@register_query(LocalMongoDBConnection)
def store_validator_set(validators_update, connection):
    space = connection.space("validators")
    _validator = space.select(validators_update["height"], index="height_search", limit=1)
    unique_id = token_hex(8) if (len(_validator.data) == 0) else _validator.data[0][0]
    space.upsert((unique_id, validators_update["height"], validators_update["validators"]),
                 op_list=[('=', 0, unique_id),
                          ('=', 1, validators_update["height"]),
                          ('=', 2, validators_update["validators"])],
                 limit=1)


@register_query(LocalMongoDBConnection)
def delete_validator_set(connection, height):
    space = connection.space("validators")
    _validators = space.select(height, index="height_search")
    for _valid in _validators.data:
        space.delete(_valid[0])


@register_query(LocalMongoDBConnection)
def store_election(election_id, height, is_concluded, connection):
    space = connection.space("elections")
    space.upsert((election_id, height, is_concluded),
                 op_list=[('=', 0, election_id),
                          ('=', 1, height),
                          ('=', 2, is_concluded)],
                 limit=1)


@register_query(LocalMongoDBConnection)
def store_elections(elections, connection):
    space = connection.space("elections")
    for election in elections:
        _election = space.insert((election["election_id"],
                                  election["height"],
                                  election["is_concluded"]))


@register_query(LocalMongoDBConnection)
def delete_elections(connection, height):
    space = connection.space("elections")
    _elections = space.select(height, index="height_search")
    for _elec in _elections.data:
        space.delete(_elec[0])


@register_query(LocalMongoDBConnection)
def get_validator_set(connection, height=None):
    space = connection.space("validators")
    _validators = space.select()
    _validators = _validators.data
    if height is not None:
        _validators = [validator for validator in _validators if validator[1] <= height]
        return next(iter(sorted(_validators, key=itemgetter(1))), None)

    return next(iter(sorted(_validators, key=itemgetter(1))), None)


@register_query(LocalMongoDBConnection)
def get_election(election_id, connection):
    space = connection.space("elections")
    _elections = space.select(election_id, index="id_search")
    _elections = _elections.data
    _election = sorted(_elections, key=itemgetter(0))[0]
    return {"election_id": _election[0], "height": _election[1], "is_concluded": _election[2]}


@register_query(LocalMongoDBConnection)
def get_asset_tokens_for_public_key(connection, asset_id, public_key):
    space = connection.space("keys")
    _keys = space.select([public_key], index="keys_search")
    space = connection.space("transactions")
    _transactions = space.select([asset_id], index="only_asset_search")
    _transactions = _transactions.data
    _keys = _keys.data
    _grouped_transactions = _group_transaction_by_ids(connection=connection, txids=[_tx[0] for _tx in _transactions])
    return _grouped_transactions


@register_query(LocalMongoDBConnection)
def store_abci_chain(height, chain_id, connection, is_synced=True):
    space = connection.space("abci_chains")
    space.upsert((height, chain_id, is_synced),
                 op_list=[('=', 0, height),
                          ('=', 1, chain_id),
                          ('=', 2, is_synced)],
                 limit=1)


@register_query(LocalMongoDBConnection)
def delete_abci_chain(connection, height):
    space = connection.space("abci_chains")
    _chains = space.select(height, index="height_search")
    for _chain in _chains.data:
        space.delete(_chain[2])


@register_query(LocalMongoDBConnection)
def get_latest_abci_chain(connection):
    space = connection.space("abci_chains")
    _all_chains = space.select()
    _chain = sorted(_all_chains.data, key=itemgetter(0))[0]
    return {"height": _chain[0], "is_synced": _chain[1], "chain_id": _chain[2]}
