import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "generate-knowledge-graph.py"
SPEC = importlib.util.spec_from_file_location("generate_knowledge_graph", MODULE_PATH)
GRAPH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GRAPH)


class TypeParsingTests(unittest.TestCase):
    def test_split_top_level_preserves_generic_arguments(self):
        self.assertEqual(
            ["ITokenService<Request, Response>", "ILogger"],
            GRAPH.split_top_level(
                "ITokenService<Request, Response>, ILogger"
            ),
        )

    def test_type_contract_key_distinguishes_generic_arity(self):
        self.assertNotEqual(
            GRAPH.type_contract_key("ITokenService"),
            GRAPH.type_contract_key("ITokenService<Request, Response>"),
        )


class DependencyResolutionTests(unittest.TestCase):
    def test_non_generic_contract_does_not_resolve_to_generic_implementation(self):
        builder = GRAPH.KnowledgeGraphBuilder(
            Path("test.sln"),
            Path("knowledge-graph.json"),
        )
        common_impl = GRAPH.ClassInfo(
            "DellIdentityClientCredAuthTokenService",
            "Common.Services",
            "ITokenService",
            "Common/Services/DellIdentityClientCredAuthTokenService.cs",
            "Common",
            "Services",
        )
        admin_impl = GRAPH.ClassInfo(
            "AuthApiService",
            "AdminTool.CmsServices",
            "ITokenService<Request, Response>",
            "AdminTool/CmsServices/AuthApiService.cs",
            "AdminTool",
            "CmsServices",
        )
        builder.all_classes = [common_impl, admin_impl]

        builder._build_lookup_maps()
        builder._build_nodes()

        self.assertEqual(
            "svc:common:services:dell-identity-client-cred-auth-token-service",
            builder._resolve_type_to_node_id("ITokenService"),
        )
        self.assertEqual(
            "svc:admin-tool:cms-services:auth-api-service",
            builder._resolve_type_to_node_id(
                "ITokenService<Request, Response>"
            ),
        )

    def test_implementation_edge_does_not_change_dependency_resolution(self):
        builder = GRAPH.KnowledgeGraphBuilder(
            Path("test.sln"),
            Path("knowledge-graph.json"),
        )
        service_contract = GRAPH.ClassInfo(
            "ICountryListByRegionService",
            "App.Services",
            "",
            "App/Services/ICountryListByRegionService.cs",
            "App",
            "Services",
        )
        service_contract.is_interface = True
        rest_contract = GRAPH.ClassInfo(
            "IRestClient",
            "Common.ExternalServices",
            "",
            "Common/ExternalServices/IRestClient.cs",
            "Common",
            "ExternalServices",
        )
        rest_contract.is_interface = True
        rest_client = GRAPH.ClassInfo(
            "RestClient",
            "Common.ExternalServices",
            "IRestClient",
            "Common/ExternalServices/RestClient.cs",
            "Common",
            "ExternalServices",
        )
        country_service = GRAPH.ClassInfo(
            "CountryListByRegionService",
            "App.Services",
            "ICountryListByRegionService",
            "App/Services/CountryListByRegionService.cs",
            "App",
            "Services",
        )
        country_service.ctor_params = [("IRestClient", "restClient")]
        builder.all_classes = [
            service_contract,
            rest_contract,
            rest_client,
            country_service,
        ]

        builder._build_lookup_maps()
        builder._build_nodes()
        builder._build_edges()

        country_id = "svc:app:services:country-list-by-region-service"
        self.assertIn(
            {
                "source": country_id,
                "target": (
                    "iface:app:services:"
                    "i-country-list-by-region-service"
                ),
                "relationship": "implements",
            },
            builder.edges,
        )
        self.assertIn(
            {
                "source": country_id,
                "target": "svc:common:external-services:rest-client",
                "relationship": "injects",
                "detail": "IRestClient restClient",
            },
            builder.edges,
        )


if __name__ == "__main__":
    unittest.main()
