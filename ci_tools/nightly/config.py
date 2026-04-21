"""Nightly CI scope definitions."""

SCOPES = {
    "sklearnex-azure": {
        "provider": "azure_pipelines",
        "azure_org": "daal",
        "azure_project": "daal4py",
        "definition_id": 20,
        "branch": "main",
        "repo": "uxlfoundation/scikit-learn-intelex",
        "upstream_repo": "scikit-learn/scikit-learn",
        "display_name": "scikit-learn-intelex nightly (Azure Pipelines)",
    },
}
