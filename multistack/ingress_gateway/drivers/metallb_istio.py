"""MetalLB + Istio ingress gateway, installed from public Helm charts.

MetalLB gives bare-metal `LoadBalancer` Services a real address (L2/ARP
mode — no BGP router required); the Istio `gateway` chart deploys the
Service that receives one and terminates traffic for whatever is routed
through it later. Four Helm releases in a fixed order: MetalLB must be up
before its IPAddressPool/L2Advertisement CRDs exist to apply against, and
istiod must exist before the gateway chart, which depends on it.
"""
from typing import Any, Dict, List

from ...helm import HelmRepository, HelmRunner
from ...kube import apply, kubectl, require_cli, require_cluster, wait_for
from ..base import IngressGatewayError, IngressGatewayPrerequisiteError
from ..spec import IngressGateway

METALLB_REPOSITORY = HelmRepository(
    name="metallb", url="https://metallb.github.io/metallb")
ISTIO_REPOSITORY = HelmRepository(
    name="istio", url="https://istio-release.storage.googleapis.com/charts")

POOL_NAME = "multistack-pool"
L2ADV_NAME = "multistack-l2adv"


class MetalLBIstioDriver:
    def __init__(self, ready_timeout: int = 300, poll_interval: int = 10) -> None:
        self._ready_timeout = ready_timeout
        self._poll_interval = poll_interval

    def _helm(self, gateway: IngressGateway) -> HelmRunner:
        return HelmRunner(
            gateway.kubeconfig_path,
            repositories=(METALLB_REPOSITORY, ISTIO_REPOSITORY),
            error_cls=IngressGatewayError,
        )

    def _pool_manifests(self, gateway: IngressGateway) -> List[Dict[str, Any]]:
        namespace = gateway.options.metallb_namespace
        return [
            {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "IPAddressPool",
                "metadata": {"name": POOL_NAME, "namespace": namespace},
                "spec": {"addresses": list(gateway.address_pool)},
            },
            {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "L2Advertisement",
                "metadata": {"name": L2ADV_NAME, "namespace": namespace},
                # nodeSelectors restricts which nodes may answer ARP for
                # this pool. Omitted, MetalLB elects from every node --
                # including one with no interface on the pool's subnet,
                # whose announcements go nowhere. See IngressGateway.
                "spec": {
                    "ipAddressPools": [POOL_NAME],
                    **({"nodeSelectors": [
                        {"matchLabels": dict(gateway.node_selector)}]}
                       if gateway.node_selector else {}),
                },
            },
        ]

    def check_prerequisites(self, gateway: IngressGateway) -> List[str]:
        require_cli("helm", error_cls=IngressGatewayPrerequisiteError)
        require_cli("kubectl", error_cls=IngressGatewayPrerequisiteError)
        require_cluster(gateway.kubeconfig_path, capability="an ingress gateway")
        # Not checkable from here -- it is a fact about the fabric the
        # nodes sit on, not about the cluster -- but it is the single
        # most likely reason a correct install is unreachable, and it
        # costs nothing to say so before an hour goes into it.
        return [
            "MetalLB layer-2 mode answers ARP for an address that belongs "
            "to no node interface. A fabric with anti-spoofing enabled "
            "drops those replies, and the result is a Service with an "
            "EXTERNAL-IP that nothing on the LAN can resolve -- no error "
            "anywhere. On OpenStack, add the pool to allowed_address_pairs "
            "on each announcing node's port; on VMware, allow forged "
            "transmits. A bare-metal LAN needs nothing."
        ]

    def create(self, gateway: IngressGateway) -> str:
        options = gateway.options
        helm = self._helm(gateway)

        helm.install_or_upgrade(
            options.metallb_release_name, chart=options.metallb_chart,
            chart_version=options.metallb_chart_version,
            namespace=options.metallb_namespace,
        )
        # The CRDs above must exist before this apply, which install_or_upgrade
        # (--wait, the HelmRunner default) already guaranteed.
        apply(
            gateway.kubeconfig_path, self._pool_manifests(gateway),
            error_cls=IngressGatewayError,
        )

        helm.install_or_upgrade(
            options.istio_base_release_name, chart=options.istio_base_chart,
            chart_version=options.istio_chart_version,
            namespace=options.istio_namespace,
        )
        helm.install_or_upgrade(
            options.istiod_release_name, chart=options.istiod_chart,
            chart_version=options.istio_chart_version,
            namespace=options.istio_namespace,
        )
        # nodeSelector, not an affinity: the constraint is hard. A node
        # outside the pool's L2 segment cannot answer ARP for the address,
        # so scheduling there produces a Service that looks assigned and
        # is unreachable -- worse than one that stays Pending and says so.
        helm.install_or_upgrade(
            options.ingress_release_name, chart=options.ingress_chart,
            chart_version=options.istio_chart_version,
            namespace=options.ingress_namespace,
            values={"nodeSelector": dict(gateway.node_selector)}
            if gateway.node_selector else None,
            # The gateway chart nests its real default (an empty mapping)
            # under an internal "_internal_defaults_do_not_set" wrapper and
            # merges this top-level key in via its own template helper --
            # Istio's documented convention for this whole chart, not a
            # typo. Strict validation only sees the wrapper, not the
            # top-level alias, so it would reject a value the chart
            # legitimately reads.
            strict_values=False,
        )

        endpoint = self._wait_for_external_ip(gateway)
        for warning in self._check_reachable(endpoint):
            print(f"[ingress_gateway] warning: {warning}")
        gateway.external_endpoint = endpoint
        return endpoint

    def delete(self, gateway: IngressGateway) -> None:
        options = gateway.options
        helm = self._helm(gateway)

        helm.uninstall(
            options.ingress_release_name, namespace=options.ingress_namespace,
            missing_ok=True,
        )
        helm.uninstall(
            options.istiod_release_name, namespace=options.istio_namespace,
            missing_ok=True,
        )
        helm.uninstall(
            options.istio_base_release_name, namespace=options.istio_namespace,
            missing_ok=True,
        )
        for kind, name in (("l2advertisement", L2ADV_NAME), ("ipaddresspool", POOL_NAME)):
            kubectl(
                gateway.kubeconfig_path, "delete", kind, name,
                "-n", options.metallb_namespace, "--ignore-not-found",
                check=False, error_cls=IngressGatewayError,
            )
        helm.uninstall(
            options.metallb_release_name, namespace=options.metallb_namespace,
            missing_ok=True,
        )

    def _check_reachable(self, endpoint: str) -> List[str]:
        """Warns when the assigned address does not accept a connection.

        MetalLB reports success once it has *allocated* an address and a
        speaker has announced it. Neither step involves a packet reaching
        the address, so an install can be entirely correct -- pool
        applied, advertisement matched, speaker logging serviceAnnounced
        -- and the gateway still be unreachable, because the fabric
        dropped the ARP reply.

        A warning, not an error: the SDK may legitimately run somewhere
        with no route to the pool's subnet, and failing there would be
        wrong. The point is that "ready" stops implying "reachable"
        silently.
        """
        import socket
        for port in (80, 443):
            probe = socket.socket()
            probe.settimeout(5)
            try:
                probe.connect((endpoint, port))
                return []
            except OSError:
                continue
            finally:
                probe.close()
        return [
            f"{endpoint} was assigned and announced, but neither port 80 "
            f"nor 443 accepted a connection from this host. If this host "
            f"is on the pool's subnet, the likely cause is fabric "
            f"anti-spoofing (see check_prerequisites) rather than "
            f"anything in the cluster -- check ARP resolves for "
            f"{endpoint} from another host on that subnet."
        ]

    def _wait_for_external_ip(self, gateway: IngressGateway) -> str:
        """Polls the ingress gateway Service until MetalLB assigns it an
        address, and returns that address.

        The `gateway` chart names the Service after its release, so the
        release name doubles as the Service name here.
        """
        options = gateway.options
        service = options.ingress_release_name
        found: Dict[str, str] = {}

        def has_ip() -> bool:
            ip = kubectl(
                gateway.kubeconfig_path, "get", "svc", service,
                "-n", options.ingress_namespace,
                "-o", "jsonpath={.status.loadBalancer.ingress[0].ip}",
                check=False, error_cls=IngressGatewayError,
            ).strip()
            if ip:
                found["ip"] = ip
                return True
            return False

        wait_for(
            has_ip, timeout=self._ready_timeout, interval=self._poll_interval,
            description=f"a MetalLB address for svc/{service}",
            error_cls=IngressGatewayError,
        )
        return found["ip"]
