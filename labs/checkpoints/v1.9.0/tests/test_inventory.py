from platformops.inventory import (
    INVENTORY,
    build_summary,
    count_by_environment,
    filter_by_environment,
    find_low_memory_prod,
    find_missing_owner,
    sort_by_name,
)


def test_find_missing_owner_flags_servers_without_an_owner_tag():
    missing = find_missing_owner(INVENTORY)
    assert "web-02" in missing
    assert "worker-01" in missing
    assert "web-01" not in missing


def test_find_low_memory_prod_flags_only_underpowered_prod_servers():
    flagged = find_low_memory_prod(INVENTORY)
    assert "web-02" in flagged
    assert "api-02" in flagged
    # a low-memory server outside prod must not be flagged
    assert "worker-02" not in flagged
    # a well-provisioned prod server must not be flagged
    assert "db-01" not in flagged


def test_count_by_environment_matches_the_inventory():
    counts = count_by_environment(INVENTORY)
    assert counts == {"prod": 6, "staging": 2, "dev": 2}
    assert sum(counts.values()) == len(INVENTORY)


def test_filter_by_environment_returns_only_that_environment():
    prod_servers = filter_by_environment(INVENTORY, "prod")
    assert len(prod_servers) == 6
    assert all(server["env"] == "prod" for server in prod_servers)


def test_sort_by_name_orders_servers_without_changing_the_original():
    original_order = [server["name"] for server in INVENTORY]

    sorted_servers = sort_by_name(INVENTORY)
    sorted_names = [server["name"] for server in sorted_servers]

    assert sorted_names == sorted(original_order)
    # the original list must be untouched -- sort_by_name must not mutate it
    assert [server["name"] for server in INVENTORY] == original_order


def test_build_summary_reports_totals_and_environment_counts():
    summary = build_summary(INVENTORY)
    assert summary["total_servers"] == 10
    assert summary["total_cpu"] == sum(server["cpu"] for server in INVENTORY)
    assert summary["total_memory_gb"] == sum(
        server["memory_gb"] for server in INVENTORY
    )
    assert summary["by_environment"] == {"prod": 6, "staging": 2, "dev": 2}
