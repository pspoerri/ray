import pytest
import sys
from typing import Dict, List, Tuple
from unittest.mock import Mock, patch, PropertyMock

from ray.exceptions import RayTaskError

from ray.serve._private.application_state import (
    ApplicationState,
    ApplicationStateManager,
)
from ray.serve._private.common import (
    ApplicationStatus,
    DeploymentConfig,
    DeploymentStatus,
    DeploymentStatusInfo,
    ReplicaConfig,
    DeploymentInfo,
)
from ray.serve._private.utils import get_random_letters
from ray.serve.exceptions import RayServeException
from ray.serve.tests.test_deployment_state import MockKVStore


class MockEndpointState:
    def __init__(self):
        self.endpoints = dict()

    def update_endpoint(self, endpoint, endpoint_info):
        self.endpoints[endpoint] = endpoint_info

    def delete_endpoint(self, endpoint):
        if endpoint in self.endpoints:
            del self.endpoints[endpoint]


class MockDeploymentStateManager:
    def __init__(self, kv_store):
        self.kv_store = kv_store
        self.deployment_infos: Dict[str, DeploymentInfo] = dict()
        self.deployment_statuses: Dict[str, DeploymentStatusInfo] = dict()
        self.deleting: Dict[str, bool] = dict()

        # Recover
        recovered_deployments = self.kv_store.get("fake_deployment_state_checkpoint")
        if recovered_deployments is not None:
            for name, checkpointed_data in recovered_deployments.items():
                (info, deleting) = checkpointed_data

                self.deployment_infos[name] = info
                self.deployment_statuses[name] = DeploymentStatus.UPDATING
                self.deleting[name] = deleting

    def deploy(self, deployment_name: str, deployment_info: DeploymentInfo):
        existing_info = self.deployment_infos.get(deployment_name)
        self.deleting[deployment_name] = False
        self.deployment_infos[deployment_name] = deployment_info
        if not existing_info or existing_info.version != deployment_info.version:
            self.deployment_statuses[deployment_name] = DeploymentStatusInfo(
                name=deployment_name,
                status=DeploymentStatus.UPDATING,
                message="",
            )

        self.kv_store.put(
            "fake_deployment_state_checkpoint",
            dict(
                zip(
                    self.deployment_infos.keys(),
                    zip(self.deployment_infos.values(), self.deleting.values()),
                )
            ),
        )

    @property
    def deployments(self) -> List[str]:
        return list(self.deployment_infos.keys())

    def get_deployment_statuses(self, deployment_names: List[str]):
        return [self.deployment_statuses[name] for name in deployment_names]

    def get_deployment(self, deployment_name: str) -> DeploymentInfo:
        if deployment_name in self.deployment_statuses:
            # Return dummy deployment info object
            return DeploymentInfo(
                deployment_config=DeploymentConfig(num_replicas=1, user_config={}),
                replica_config=ReplicaConfig.create(lambda x: x),
                start_time_ms=0,
                deployer_job_id="",
            )

    def get_deployments_in_application(self, app_name: str):
        return [
            name
            for name, info in self.deployment_infos.items()
            if info.app_name == app_name
        ]

    def set_deployment_unhealthy(self, name: str):
        self.deployment_statuses[name].status = DeploymentStatus.UNHEALTHY

    def set_deployment_healthy(self, name: str):
        self.deployment_statuses[name].status = DeploymentStatus.HEALTHY

    def set_deployment_updating(self, name: str):
        self.deployment_statuses[name].status = DeploymentStatus.UPDATING

    def set_deployment_deleted(self, name: str):
        if not self.deployment_infos[name]:
            raise ValueError(
                f"Tried to mark deployment {name} as deleted, but {name} not found"
            )
        if not self.deleting[name]:
            raise ValueError(
                f"Tried to mark deployment {name} as deleted, but delete_deployment()"
                f"hasn't been called for {name} yet"
            )

        del self.deployment_infos[name]
        del self.deployment_statuses[name]
        del self.deleting[name]

    def set_deployment(
        self,
        deployment_name: str,
        deployment_info: DeploymentInfo,
        status: DeploymentStatus,
    ):
        self.deleting[deployment_name] = False
        self.deployment_infos[deployment_name] = deployment_info
        self.deployment_statuses[deployment_name] = DeploymentStatusInfo(
            name=deployment_name,
            status=status,
            message="",
        )

    def delete_deployment(self, deployment_name: str):
        self.deleting[deployment_name] = True


@pytest.fixture
def mocked_application_state_manager() -> Tuple[
    ApplicationStateManager, MockDeploymentStateManager
]:
    kv_store = MockKVStore()

    deployment_state_manager = MockDeploymentStateManager(kv_store)
    application_state_manager = ApplicationStateManager(
        deployment_state_manager, MockEndpointState(), kv_store
    )
    yield application_state_manager, deployment_state_manager, kv_store


def deployment_params(name: str, route_prefix: str = None):
    return {
        "name": name,
        "deployment_config_proto_bytes": DeploymentConfig(
            num_replicas=1, user_config={}, version=get_random_letters()
        ).to_proto_bytes(),
        "replica_config_proto_bytes": ReplicaConfig.create(
            lambda x: x
        ).to_proto_bytes(),
        "deployer_job_id": "random",
        "route_prefix": route_prefix,
        "docs_path": None,
        "is_driver_deployment": False,
    }


@pytest.fixture
def mocked_application_state() -> Tuple[ApplicationState, MockDeploymentStateManager]:
    kv_store = MockKVStore()

    deployment_state_manager = MockDeploymentStateManager(kv_store)
    application_state = ApplicationState(
        "test_app",
        deployment_state_manager,
        MockEndpointState(),
        lambda *args, **kwargs: None,
    )
    yield application_state, deployment_state_manager


@patch.object(
    ApplicationState, "target_deployments", PropertyMock(return_value=["a", "b", "c"])
)
class TestDetermineAppStatus:
    @patch.object(ApplicationState, "get_deployments_statuses")
    def test_running(self, get_deployments_statuses, mocked_application_state):
        app_state, _ = mocked_application_state
        get_deployments_statuses.return_value = [
            DeploymentStatusInfo("a", DeploymentStatus.HEALTHY),
            DeploymentStatusInfo("b", DeploymentStatus.HEALTHY),
            DeploymentStatusInfo("c", DeploymentStatus.HEALTHY),
        ]
        assert app_state._determine_app_status() == (ApplicationStatus.RUNNING, "")

    @patch.object(ApplicationState, "get_deployments_statuses")
    def test_stay_running(self, get_deployments_statuses, mocked_application_state):
        app_state, _ = mocked_application_state
        app_state._status = ApplicationStatus.RUNNING
        get_deployments_statuses.return_value = [
            DeploymentStatusInfo("a", DeploymentStatus.HEALTHY),
            DeploymentStatusInfo("b", DeploymentStatus.HEALTHY),
            DeploymentStatusInfo("c", DeploymentStatus.HEALTHY),
        ]
        assert app_state._determine_app_status() == (ApplicationStatus.RUNNING, "")

    @patch.object(ApplicationState, "get_deployments_statuses")
    def test_deploying(self, get_deployments_statuses, mocked_application_state):
        app_state, _ = mocked_application_state
        get_deployments_statuses.return_value = [
            DeploymentStatusInfo("a", DeploymentStatus.UPDATING),
            DeploymentStatusInfo("b", DeploymentStatus.HEALTHY),
            DeploymentStatusInfo("c", DeploymentStatus.HEALTHY),
        ]
        assert app_state._determine_app_status() == (ApplicationStatus.DEPLOYING, "")

    @patch.object(ApplicationState, "get_deployments_statuses")
    def test_deploy_failed(self, get_deployments_statuses, mocked_application_state):
        app_state, _ = mocked_application_state
        get_deployments_statuses.return_value = [
            DeploymentStatusInfo("a", DeploymentStatus.UPDATING),
            DeploymentStatusInfo("b", DeploymentStatus.HEALTHY),
            DeploymentStatusInfo("c", DeploymentStatus.UNHEALTHY),
        ]
        status, error_msg = app_state._determine_app_status()
        assert status == ApplicationStatus.DEPLOY_FAILED
        assert error_msg

    @patch.object(ApplicationState, "get_deployments_statuses")
    def test_unhealthy(self, get_deployments_statuses, mocked_application_state):
        app_state, _ = mocked_application_state
        app_state._status = ApplicationStatus.RUNNING
        get_deployments_statuses.return_value = [
            DeploymentStatusInfo("a", DeploymentStatus.HEALTHY),
            DeploymentStatusInfo("b", DeploymentStatus.HEALTHY),
            DeploymentStatusInfo("c", DeploymentStatus.UNHEALTHY),
        ]
        status, error_msg = app_state._determine_app_status()
        assert status == ApplicationStatus.UNHEALTHY
        assert error_msg


def test_deploy_and_delete_app(mocked_application_state):
    """Deploy app with 2 deployments, transition DEPLOYING -> RUNNING -> DELETING.
    This tests the basic typical workflow.
    """
    app_state, deployment_state_manager = mocked_application_state

    # DEPLOY application with deployments {d1, d2}
    app_state.apply_deployment_args([deployment_params("d1"), deployment_params("d2")])

    app_status = app_state.get_application_status_info()
    assert app_status.status == ApplicationStatus.DEPLOYING
    assert app_status.deployment_timestamp > 0

    app_state.update()
    # After one update, deployments {d1, d2} should be created
    assert deployment_state_manager.get_deployment("d1")
    assert deployment_state_manager.get_deployment("d2")
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Until both deployments are healthy, app should be deploying
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Mark deployment d1 healthy (app should be deploying)
    deployment_state_manager.set_deployment_healthy("d1")
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Mark deployment d2 healthy (app should be running)
    deployment_state_manager.set_deployment_healthy("d2")
    app_state.update()
    assert app_state.status == ApplicationStatus.RUNNING

    # Rerun update, status shouldn't change (still running)
    app_state.update()
    assert app_state.status == ApplicationStatus.RUNNING

    # Delete application (app should be deleting)
    app_state.delete()
    assert app_state.status == ApplicationStatus.DELETING

    app_state.update()
    deployment_state_manager.set_deployment_deleted("d1")
    ready_to_be_deleted = app_state.update()
    assert not ready_to_be_deleted
    assert app_state.status == ApplicationStatus.DELETING

    # Once both deployments are deleted, the app should be ready to delete
    deployment_state_manager.set_deployment_deleted("d2")
    ready_to_be_deleted = app_state.update()
    assert ready_to_be_deleted


def test_app_deploy_failed_and_redeploy(mocked_application_state):
    """Test DEPLOYING -> DEPLOY_FAILED -> (redeploy) -> DEPLOYING -> RUNNING"""
    app_state, deployment_state_manager = mocked_application_state
    app_state.apply_deployment_args([deployment_params("d1")])
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Before status of deployment changes, app should still be DEPLOYING
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Mark deployment unhealthy -> app should be DEPLOY_FAILED
    deployment_state_manager.set_deployment_unhealthy("d1")
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOY_FAILED

    # Message and status should not change
    deploy_failed_msg = app_state._status_msg
    assert len(deploy_failed_msg) != 0
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOY_FAILED
    assert app_state._status_msg == deploy_failed_msg

    app_state.apply_deployment_args([deployment_params("d1"), deployment_params("d2")])
    assert app_state.status == ApplicationStatus.DEPLOYING
    assert app_state._status_msg != deploy_failed_msg

    # After one update, deployments {d1, d2} should be created
    app_state.update()
    assert deployment_state_manager.get_deployment("d1")
    assert deployment_state_manager.get_deployment("d2")
    assert app_state.status == ApplicationStatus.DEPLOYING

    deployment_state_manager.set_deployment_healthy("d1")
    deployment_state_manager.set_deployment_healthy("d2")
    app_state.update()
    assert app_state.status == ApplicationStatus.RUNNING

    # Message and status should not change
    running_msg = app_state._status_msg
    assert running_msg != deploy_failed_msg
    app_state.update()
    assert app_state.status == ApplicationStatus.RUNNING
    assert app_state._status_msg == running_msg


def test_app_deploy_failed_and_recover(mocked_application_state):
    """Test DEPLOYING -> DEPLOY_FAILED -> (self recovered) -> RUNNING

    If while the application is deploying a deployment becomes unhealthy,
    the app is marked as deploy failed. But if the deployment recovers,
    the application status should update to running.
    """
    app_state, deployment_state_manager = mocked_application_state
    app_state.apply_deployment_args([deployment_params("d1")])
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Before status of deployment changes, app should still be DEPLOYING
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Mark deployment unhealthy -> app should be DEPLOY_FAILED
    deployment_state_manager.set_deployment_unhealthy("d1")
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOY_FAILED
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOY_FAILED

    # Deployment recovers to healthy -> app should be RUNNING
    deployment_state_manager.set_deployment_healthy("d1")
    app_state.update()
    assert app_state.status == ApplicationStatus.RUNNING
    app_state.update()
    assert app_state.status == ApplicationStatus.RUNNING


def test_app_unhealthy(mocked_application_state):
    """Test DEPLOYING -> RUNNING -> UNHEALTHY -> RUNNING.
    Even after an application becomes running, if a deployment becomes
    unhealthy at some point, the application status should also be
    updated to unhealthy.
    """
    app_state, deployment_state_manager = mocked_application_state
    app_state.apply_deployment_args([deployment_params("a"), deployment_params("b")])
    assert app_state.status == ApplicationStatus.DEPLOYING
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Once both deployments become healthy, app should be running
    deployment_state_manager.set_deployment_healthy("a")
    deployment_state_manager.set_deployment_healthy("b")
    app_state.update()
    assert app_state.status == ApplicationStatus.RUNNING

    # If a deployment becomes unhealthy, application should become unhealthy
    deployment_state_manager.set_deployment_unhealthy("a")
    app_state.update()
    assert app_state.status == ApplicationStatus.UNHEALTHY
    # Rerunning update shouldn't make a difference
    app_state.update()
    assert app_state.status == ApplicationStatus.UNHEALTHY

    # If the deployment recovers, the application should also recover
    deployment_state_manager.set_deployment_healthy("a")
    app_state.update()
    assert app_state.status == ApplicationStatus.RUNNING


@patch("ray.serve._private.application_state.check_obj_ref_ready_nowait")
@patch("ray.get")
def test_deploy_through_config_succeed(get, check_obj_ref_ready_nowait):
    """Test deploying through config successfully.
    Deploy obj ref finishes successfully, so status should transition to running.
    """
    kv_store = MockKVStore()
    deployment_state_manager = MockDeploymentStateManager(kv_store)
    app_state_manager = ApplicationStateManager(
        deployment_state_manager, MockEndpointState(), kv_store
    )
    # Create application state
    app_state_manager.create_application_state(name="test_app", deploy_obj_ref=Mock())
    app_state = app_state_manager._application_states["test_app"]
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Before object ref is ready
    check_obj_ref_ready_nowait.return_value = False
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Object ref is ready, and the task has called apply_deployment_args
    check_obj_ref_ready_nowait.return_value = True
    app_state.apply_deployment_args([deployment_params("a")])
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOYING
    assert app_state.target_deployments == ["a"]

    # Set healthy
    deployment_state_manager.set_deployment_healthy("a")
    app_state.update()
    assert app_state.status == ApplicationStatus.RUNNING


@patch("ray.serve._private.application_state.check_obj_ref_ready_nowait")
@patch("ray.get", side_effect=RayTaskError(None, "intentionally failed", None))
def test_deploy_through_config_fail(get, check_obj_ref_ready_nowait):
    """Test fail to deploy through config.
    Deploy obj ref errors out, so status should transition to deploy failed.
    """
    kv_store = MockKVStore()
    deployment_state_manager = MockDeploymentStateManager(kv_store)
    app_state_manager = ApplicationStateManager(
        deployment_state_manager, MockEndpointState(), kv_store
    )
    # Create application state
    app_state_manager.create_application_state(name="test_app", deploy_obj_ref=Mock())
    app_state = app_state_manager._application_states["test_app"]
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Before object ref is ready
    check_obj_ref_ready_nowait.return_value = False
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Object ref is ready, and the task has called apply_deployment_args
    check_obj_ref_ready_nowait.return_value = True
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOY_FAILED
    assert "failed" in app_state._status_msg or "error" in app_state._status_msg


def test_redeploy_same_app(mocked_application_state):
    """Test redeploying same application with updated deployments."""
    app_state, deployment_state_manager = mocked_application_state
    app_state.apply_deployment_args([deployment_params("a"), deployment_params("b")])
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Update
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOYING
    assert set(app_state.target_deployments) == {"a", "b"}

    # Transition to running
    deployment_state_manager.set_deployment_healthy("a")
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOYING
    deployment_state_manager.set_deployment_healthy("b")
    app_state.update()
    assert app_state.status == ApplicationStatus.RUNNING

    # Deploy the same app with different deployments
    app_state.apply_deployment_args([deployment_params("b"), deployment_params("c")])
    assert app_state.status == ApplicationStatus.DEPLOYING
    # Target state should be updated immediately
    assert "a" not in app_state.target_deployments

    # Remove deployment `a`
    app_state.update()
    deployment_state_manager.set_deployment_deleted("a")
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Move to running
    deployment_state_manager.set_deployment_healthy("c")
    app_state.update()
    assert app_state.status == ApplicationStatus.DEPLOYING
    deployment_state_manager.set_deployment_healthy("b")
    app_state.update()
    assert app_state.status == ApplicationStatus.RUNNING


def test_deploy_with_route_prefix_conflict(mocked_application_state_manager):
    """Test that an application with a route prefix conflict fails to deploy"""
    app_state_manager, _, _ = mocked_application_state_manager

    app_state_manager.apply_deployment_args("app1", [deployment_params("a", "/hi")])
    with pytest.raises(RayServeException):
        app_state_manager.apply_deployment_args("app2", [deployment_params("b", "/hi")])


def test_deploy_with_renamed_app(mocked_application_state_manager):
    """
    Test that an application deploys successfully when there is a route prefix
    conflict with an old app running on the cluster.
    """
    app_state_manager, deployment_state_manager, _ = mocked_application_state_manager

    # deploy app1
    app_state_manager.apply_deployment_args("app1", [deployment_params("a", "/url1")])
    app_state = app_state_manager._application_states["app1"]
    assert app_state_manager.get_app_status("app1") == ApplicationStatus.DEPLOYING

    # Update
    app_state_manager.update()
    assert app_state_manager.get_app_status("app1") == ApplicationStatus.DEPLOYING
    assert set(app_state.target_deployments) == {"a"}

    # Once its single deployment is healthy, app1 should be running
    deployment_state_manager.set_deployment_healthy("a")
    app_state_manager.update()
    assert app_state_manager.get_app_status("app1") == ApplicationStatus.RUNNING

    # delete app1
    app_state_manager.delete_application("app1")
    assert app_state_manager.get_app_status("app1") == ApplicationStatus.DELETING
    app_state_manager.update()

    # deploy app2
    app_state_manager.apply_deployment_args("app2", [deployment_params("b", "/url1")])
    assert app_state_manager.get_app_status("app2") == ApplicationStatus.DEPLOYING
    app_state_manager.update()

    # app2 deploys before app1 finishes deleting
    deployment_state_manager.set_deployment_healthy("b")
    app_state_manager.update()
    assert app_state_manager.get_app_status("app2") == ApplicationStatus.RUNNING
    assert app_state_manager.get_app_status("app1") == ApplicationStatus.DELETING

    # app1 finally finishes deleting
    deployment_state_manager.set_deployment_deleted("a")
    app_state_manager.update()
    assert app_state_manager.get_app_status("app1") == ApplicationStatus.NOT_STARTED
    assert app_state_manager.get_app_status("app2") == ApplicationStatus.RUNNING


def test_application_state_recovery(mocked_application_state_manager):
    """Test DEPLOYING -> RUNNING -> (controller crash) -> DEPLOYING -> RUNNING"""
    (
        app_state_manager,
        deployment_state_manager,
        kv_store,
    ) = mocked_application_state_manager
    app_name = "test_app"

    # DEPLOY application with deployments {d1, d2}
    params = deployment_params("d1")
    app_state_manager.apply_deployment_args(app_name, [params])
    app_state = app_state_manager._application_states[app_name]
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Once deployment is healthy, app should be running
    app_state_manager.update()
    assert deployment_state_manager.get_deployment("d1")
    deployment_state_manager.set_deployment_healthy("d1")
    app_state_manager.update()
    assert app_state.status == ApplicationStatus.RUNNING

    # Simulate controller crashed!! Create new deployment state manager,
    # which should recover target state for deployment "d1" from kv store
    new_deployment_state_manager = MockDeploymentStateManager(kv_store)
    version1 = new_deployment_state_manager.deployment_infos["d1"].version

    # Create new application state manager, and it should call _recover_from_checkpoint
    new_app_state_manager = ApplicationStateManager(
        new_deployment_state_manager, MockEndpointState(), kv_store
    )
    app_state = new_app_state_manager._application_states[app_name]
    assert app_state.status == ApplicationStatus.DEPLOYING
    assert app_state._target_state.deployment_infos["d1"].version == version1

    new_deployment_state_manager.set_deployment_healthy("d1")
    new_app_state_manager.update()
    assert app_state.status == ApplicationStatus.RUNNING


def test_recover_during_update(mocked_application_state_manager):
    """Test that application and deployment states are recovered if
    controller crashed in the middle of a redeploy.

    Target state is checkpointed in the application state manager,
    but not yet the deployment state manager when the controller crashes
    Then the deployment state manager should recover an old version of
    the deployment during initial recovery, but the application state
    manager should eventually reconcile this.
    """
    (
        app_state_manager,
        deployment_state_manager,
        kv_store,
    ) = mocked_application_state_manager
    app_name = "test_app"

    # DEPLOY application with deployment "d1"
    params = deployment_params("d1")
    app_state_manager.apply_deployment_args(app_name, [params])
    app_state = app_state_manager._application_states[app_name]
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Once deployment is healthy, app should be running
    app_state_manager.update()
    assert deployment_state_manager.get_deployment("d1")
    deployment_state_manager.set_deployment_healthy("d1")
    app_state_manager.update()
    assert app_state.status == ApplicationStatus.RUNNING

    # Deploy new version of "d1" (this auto generates new random version)
    params2 = deployment_params("d1")
    app_state_manager.apply_deployment_args(app_name, [params2])
    assert app_state.status == ApplicationStatus.DEPLOYING

    # Before application state manager could propagate new version to
    # deployment state manager, controller crashes.
    # Create new deployment state manager. It should recover the old
    # version of the deployment from the kv store
    new_deployment_state_manager = MockDeploymentStateManager(kv_store)
    dr_version = new_deployment_state_manager.deployment_infos["d1"].version

    # Create new application state manager, and it should call _recover_from_checkpoint
    new_app_state_manager = ApplicationStateManager(
        new_deployment_state_manager, MockEndpointState(), kv_store
    )
    app_state = new_app_state_manager._application_states[app_name]
    ar_version = app_state._target_state.deployment_infos["d1"].version
    assert app_state.status == ApplicationStatus.DEPLOYING
    assert ar_version != dr_version

    new_app_state_manager.update()
    assert new_deployment_state_manager.deployment_infos["d1"].version == ar_version
    assert app_state.status == ApplicationStatus.DEPLOYING

    new_deployment_state_manager.set_deployment_healthy("d1")
    new_app_state_manager.update()
    assert app_state.status == ApplicationStatus.RUNNING


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", "-s", __file__]))
