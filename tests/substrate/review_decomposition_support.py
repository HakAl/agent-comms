def require_schema_v1(contract):
    version = contract.get("schema_version")
    if type(version) is not int or version != 1:
        raise ValueError(f"contract schema_version must be int 1, got {version!r}")
    return contract
