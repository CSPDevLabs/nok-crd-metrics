import time
from prometheus_client import start_http_server, Gauge, REGISTRY, PROCESS_COLLECTOR, GC_COLLECTOR, PLATFORM_COLLECTOR
from kubernetes import client, config

# 1. Clean up default metrics
REGISTRY.unregister(PROCESS_COLLECTOR)
REGISTRY.unregister(GC_COLLECTOR)
REGISTRY.unregister(PLATFORM_COLLECTOR)

# Metrics Definitions
DEVIATION_COUNT = Gauge(
    'sdcio_deviation_count', 
    'Number of deviations found in the spec per target', 
    ['target_name', 'namespace', 'name', 'deviation_type']
)

TARGET_CONFIG_READY = Gauge(
    'sdcio_target_config_ready',
    'Target ConfigReady status (1 for True, 0 for False)',
    ['name', 'namespace', 'address', 'vendor']
)

# New Metric for Config status
CONFIG_READY = Gauge(
    'sdcio_config_ready',
    'Config Ready status (1 for True, 0 for False)',
    ['name', 'namespace', 'target_name']
)

def monitor_resources():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    custom_api = client.CustomObjectsApi()
    print("Starting sdcio-metrics exporter on port 8080...")
    start_http_server(8080)

    while True:
        try:
            # --- 1. PROCESS DEVIATIONS (config.sdcio.dev) ---
            deviations = custom_api.list_namespaced_custom_object(
                group="config.sdcio.dev", version="v1alpha1",
                namespace="nok-bng", plural="deviations"
            )
            DEVIATION_COUNT.clear()
            for item in deviations.get('items', []):
                meta = item.get('metadata', {})
                spec = item.get('spec', {})
                target = meta.get('labels', {}).get('config.sdcio.dev/targetName', meta.get('name'))
                DEVIATION_COUNT.labels(
                    target_name=target, 
                    namespace=meta.get('namespace', 'nok-bng'), 
                    name=meta.get('name', 'unknown'), 
                    deviation_type=spec.get('deviationType', 'unknown')
                ).set(len(spec.get('deviations', [])))

            # --- 2. PROCESS TARGETS (inv.sdcio.dev) ---
            targets = custom_api.list_namespaced_custom_object(
                group="inv.sdcio.dev", version="v1alpha1",
                namespace="nok-bng", plural="targets"
            )
            TARGET_CONFIG_READY.clear()
            for item in targets.get('items', []):
                meta = item.get('metadata', {})
                spec = item.get('spec', {})
                status = item.get('status', {})
                is_ready = 1 if any(c.get('type') == 'ConfigReady' and c.get('status') == 'True' for c in status.get('conditions', [])) else 0
                TARGET_CONFIG_READY.labels(
                    name=meta.get('name', 'unknown'), 
                    namespace=meta.get('namespace', 'nok-bng'), 
                    address=spec.get('address', 'unknown'), 
                    vendor=meta.get('labels', {}).get('vendor', 'unknown')
                ).set(is_ready)

            # --- 3. PROCESS CONFIGS (config.sdcio.dev) ---
            configs = custom_api.list_namespaced_custom_object(
                group="config.sdcio.dev", version="v1alpha1",
                namespace="nok-bng", plural="configs"
            )
            CONFIG_READY.clear()
            for item in configs.get('items', []):
                meta = item.get('metadata', {})
                status = item.get('status', {})
                
                # Labels
                c_name = meta.get('name', 'unknown')
                c_namespace = meta.get('namespace', 'nok-bng')
                c_target = meta.get('labels', {}).get('config.sdcio.dev/targetName', 'unknown')

                # Check Ready condition
                c_is_ready = 1 if any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in status.get('conditions', [])) else 0
                
                CONFIG_READY.labels(
                    name=c_name, 
                    namespace=c_namespace, 
                    target_name=c_target
                ).set(c_is_ready)

            time.sleep(30)
            
        except Exception as e:
            print(f"Error fetching resources: {e}")
            time.sleep(10)

if __name__ == '__main__':
    monitor_resources()
