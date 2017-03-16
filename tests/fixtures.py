sample_basic_test_plan = """
{
  "ecs_name": "ardere-test",
  "name": "Loadtest",
  "description": "Run all APLT scenarios",
  "steps": [
    {
      "name": "TestCluster",
      "instance_count": 1,
      "instance_type": "t2.medium",
      "run_max_time": 140,
      "cpu_units": 2048,
      "container_name": "bbangert/ap-loadtester:latest",
      "additional_command_args": "./apenv/bin/aplt_testplan wss://autopush.stage.mozaws.net 'aplt.scenarios:notification_forever,1000,1,0'"
    }
  ]
}
"""

future_hypothetical_test="""
{
    "name": "TestCluster",
    "instance_count": 1,
    "instance_type": "t2.medium",
    "run_max_time": 140,
    "cpu_units": 2048,
    "container_name": "bbangert/pushgo:1.5rc4",
    "port_mapping": "8080:8090,8081:8081,3000:3000,8082:8082",
    "load_balancer": {
        "env_var": "TEST_CLUSTER",
        "ping_path": "/status/health",
        "ping_port": 8080,
        "ping_protocol": "http",
        "listeners": [
            {
                "listen_protocol": "ssl",
                "listen_port": 443,
                "backend_protocol": "tcp",
                "backend_port": 8080
            },
            {
                "listen_protocol": "https",
                "listen_port": 9000,
                "backend_protocol": "http",
                "backend_port": 8090
            }
        ]
    }
}
"""