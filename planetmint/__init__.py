# Copyright © 2020 Interplanetary Database Association e.V.,
# Planetmint and IPDB software contributors.
# SPDX-License-Identifier: (Apache-2.0 AND CC-BY-4.0)
# Code is Apache-2.0 and docs are CC-BY-4.0

from planetmint.common.transaction import Transaction  # noqa
from planetmint import models  # noqa
from planetmint.upsert_validator import ValidatorElection  # noqa
from planetmint.elections.vote import Vote  # noqa
from planetmint.migrations.chain_migration_election import ChainMigrationElection
from planetmint.lib import Planetmint

Transaction.register_type(Transaction.CREATE, models.Transaction)
Transaction.register_type(Transaction.TRANSFER, models.Transaction)
Transaction.register_type(ValidatorElection.OPERATION, ValidatorElection)
Transaction.register_type(ChainMigrationElection.OPERATION, ChainMigrationElection)
Transaction.register_type(Vote.OPERATION, Vote)
