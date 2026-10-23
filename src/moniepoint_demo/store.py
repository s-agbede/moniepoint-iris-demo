"""Small Redis persistence boundary for a typed demo world."""

from collections.abc import Callable

from redis import Redis, WatchError

from moniepoint_demo.fixtures import CUSTOMER_IDS, demo_world
from moniepoint_demo.models import NewTransaction, World
from moniepoint_demo.simulator import DomainError, create_transaction


class RedisWorldStore:
    def __init__(
        self,
        client: Redis,
        key: str = "moniepoint-demo:world",
        customer_id: str = "customer-001",
    ) -> None:
        self.client = client
        self.key = key
        self.customer_id = customer_id

    def initialize(self) -> None:
        fixture = demo_world(self.customer_id)
        if self.client.set(self.key, fixture.model_dump_json(), nx=True):
            return

        def add_missing_history(world: World) -> None:
            # Preserve existing presenter changes while enriching older demo worlds.
            existing_ids = {tx.transaction_id for tx in world.transactions}
            for tx in fixture.transactions:
                if tx.transaction_id not in existing_ids:
                    create_transaction(
                        world,
                        NewTransaction.model_validate(
                            tx.model_dump(exclude={"customer_id"})
                        ),
                    )

        self.mutate(add_missing_history)

    def for_customer(self, customer_id: str) -> "RedisWorldStore":
        if customer_id not in CUSTOMER_IDS:
            raise DomainError("Demo customer not found", 404)
        if customer_id == self.customer_id:
            return self
        return RedisWorldStore(self.client, f"{self.key}:{customer_id}", customer_id)

    def read(self) -> World:
        raw = self.client.get(self.key)
        if not isinstance(raw, (str, bytes)):
            raise DomainError("Demo world is missing; reset the simulator", 503)
        return World.model_validate_json(raw)

    def mutate[T](self, change: Callable[[World], T]) -> T:
        for _ in range(5):
            with self.client.pipeline() as pipe:
                try:
                    pipe.watch(self.key)  # type: ignore[no-untyped-call]
                    raw = pipe.get(self.key)
                    if not isinstance(raw, (str, bytes)):
                        raise DomainError("Demo world is missing", 503)
                    world = World.model_validate_json(raw)
                    result = change(world)
                    pipe.multi()
                    pipe.set(self.key, world.model_dump_json())
                    pipe.execute()
                    return result
                except WatchError:
                    continue
        raise DomainError("Concurrent change; retry the request")

    def reset(self) -> World:
        world = demo_world(self.customer_id)
        self.client.set(self.key, world.model_dump_json())
        return world
