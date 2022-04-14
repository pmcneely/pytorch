
# Owner(s): ["oncall: distributed"]
import sys
import copy

import torch
import torch.nn as nn
from torch.testing._internal.common_distributed import (
    requires_nccl,
    skip_if_lt_x_gpu,
)
from torch.distributed._shard import shard_module
from torch.distributed._shard.sharding_plan import ShardingPlan
from torch.distributed._shard.sharder import Sharder
from torch.distributed._shard.sharding_spec import ChunkShardingSpec
from torch.distributed._shard.sharded_tensor import ShardedTensor

from torch.testing._internal.common_utils import TEST_WITH_DEV_DBG_ASAN
from torch.testing._internal.distributed._shard.sharded_tensor import (
    TEST_GPU_NUM,
    ShardedTensorTestBase,
    with_comms,
)
from torch.testing._internal.distributed._shard.sharded_tensor._test_ops_common import (
    generate_chunk_sharding_specs_for_test,
)
from torch.testing._internal.distributed._shard.test_common import SimpleMegatronLM

if TEST_WITH_DEV_DBG_ASAN:
    print(
        "Skip dev-asan as torch + multiprocessing spawn have known issues",
        file=sys.stderr,
    )
    sys.exit(0)




class TestCustomSharder(ShardedTensorTestBase):

    @with_comms(init_rpc=False)
    @skip_if_lt_x_gpu(TEST_GPU_NUM)
    @requires_nccl()
    def test_basic_sharding_plan(self):
        colwise_sharding_spec = generate_chunk_sharding_specs_for_test(0)
        rowwise_sharding_spec = generate_chunk_sharding_specs_for_test(1)
        for spec in zip(colwise_sharding_spec, rowwise_sharding_spec):
            # test each sharding spec pair and see if we can apply sharding
            reshard_spec = copy.deepcopy(spec[1])
            reshard_spec.placements.sort(key=lambda placement: placement.rank())
            reshard_spec.dim = 0

            sharding_plan = ShardingPlan(
                plan={
                    "fc1.weight": spec[0],
                    "fc2.weight": spec[1]
                },
                output_plan={
                    "": reshard_spec
                },
                collect_local_shards=[""])

            # Use same seed.
            torch.manual_seed(0)
            local_megatron_lm = SimpleMegatronLM([[17, 12], [12, 29]]).cuda(self.rank)
            megatron_lm = copy.deepcopy(local_megatron_lm)

            # shard the module with the provided sharding plan
            shard_module(megatron_lm, sharding_plan)

            # check to make sure the module already been sharded
            self.assertTrue(isinstance(megatron_lm.fc1.weight, ShardedTensor))
            self.assertTrue(isinstance(megatron_lm.fc2.weight, ShardedTensor))
            self.assertEqual(megatron_lm.fc1.weight.sharding_spec(), spec[0])
            self.assertEqual(megatron_lm.fc2.weight.sharding_spec(), spec[1])

            # make sure we can run sharded computation
            input = torch.rand(22, 17).cuda(self.rank)
            sharded_output = megatron_lm(input)
            local_output = local_megatron_lm(input)

            # verify and make sure local and sharded output matches
            self.assertEqual(local_output, sharded_output)
