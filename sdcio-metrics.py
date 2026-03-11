import time
from prometheus_client import start_http_server, Gauge
from kubernetes import client, config, watch

# Define Prometheus metrics
# We use labels so you can filter by target or type in Grafana
DEVIATION_STATUS = Gauge(
    'sdcio_deviation_info', 
    'Current deviation status per target', 
    ['target_name', 'namespace', 'deviation_type']
)

def monitor_deviations():
    # Load config from the Pod's ServiceAccount
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    custom_api = client.CustomObjectsApi()
    
    print("Starting sdcio-metrics exporter on port 8080...")
    start_http_server(8080)

    while True:
        try:
            # List current deviations in the namespace
            resource = custom_api.list_namespaced_custom_object(
                group="config.sdcio.dev",
                version="v1alpha1",
                namespace="nok-bng",
                plural="deviations"
            )
            
            # Clear old metrics to handle deleted deviations
            DEVIATION_STATUS.clear()

            for item in resource.get('items', []):
                metadata = item.get('metadata', {})
                spec = item.get('spec', {})
                
                # Extract labels based on your JSON structure
                target = metadata.get('labels', {}).get('config.sdcio.dev/targetName', 'unknown')
                d_type = spec.get('deviationType', 'unknown')
                namespace = metadata.get('namespace', 'nok-bng')
                
                # Setting value to 1 (active). 
                # If your CRD eventually has a "count" field in status, 
                # you can use item.get('status', {}).get('count', 1)
                DEVIATION_STATUS.labels(
                    target_name=target, 
                    namespace=namespace, 
                    deviation_type=d_type
                ).set(1)

            time.sleep(30) # Poll every 30 seconds
            
        except Exception as e:
            print(f"Error fetching deviations: {e}")
            time.sleep(10)

if __name__ == '__main__':
    monitor_deviations()
