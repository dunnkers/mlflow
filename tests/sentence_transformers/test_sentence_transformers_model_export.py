import json
import numpy as np
import os
import pandas as pd
from sentence_transformers import SentenceTransformer
import pytest
from unittest import mock
import yaml

import mlflow

import mlflow.pyfunc.scoring_server as pyfunc_scoring_server
import mlflow.sentence_transformers
from mlflow import pyfunc
from mlflow.models import Model, infer_signature
from mlflow.models.utils import _read_example
from mlflow.store.artifact.s3_artifact_repo import S3ArtifactRepository
from mlflow.tracking._model_registry import DEFAULT_AWAIT_MAX_SLEEP_SECONDS
from mlflow.utils.environment import _mlflow_conda_env
from pyspark.sql import SparkSession
from pyspark.sql.types import ArrayType, DoubleType

from tests.helper_functions import (
    _assert_pip_requirements,
    _compare_logged_code_paths,
    _mlflow_major_version_string,
    pyfunc_serve_and_score_model,
)


@pytest.fixture
def model_path(tmp_path):
    return tmp_path.joinpath("model")


@pytest.fixture
def basic_model():
    return SentenceTransformer("all-MiniLM-L6-v2")


@pytest.fixture(scope="module")
def spark():
    with SparkSession.builder.master("local[1]").getOrCreate() as s:
        yield s


def test_model_save_and_load(model_path, basic_model):
    mlflow.sentence_transformers.save_model(model=basic_model, path=model_path)

    loaded_model = mlflow.sentence_transformers.load_model(model_path)

    encoded_single = loaded_model.encode("I'm just a simple string; nothing to see here.")
    encoded_multi = loaded_model.encode(["I'm a string", "I'm also a string", "Please encode me"])

    assert isinstance(encoded_single, np.ndarray)
    assert len(encoded_single) == 384
    assert isinstance(encoded_multi, np.ndarray)
    assert len(encoded_multi) == 3
    assert all(len(x) == 384 for x in encoded_multi)


def test_dependency_mapping(model_path, basic_model):
    pip_requirements = mlflow.sentence_transformers.get_default_pip_requirements()

    expected_requirements = {"sentence-transformers", "torch", "transformers"}
    assert {package.split("=")[0] for package in pip_requirements}.intersection(
        expected_requirements
    ) == expected_requirements

    conda_requirements = mlflow.sentence_transformers.get_default_conda_env()
    pip_in_conda = {
        package.split("=")[0] for package in conda_requirements["dependencies"][2]["pip"]
    }
    expected_conda = {"mlflow"}
    expected_conda.update(expected_requirements)
    assert pip_in_conda.intersection(expected_conda) == expected_conda


def test_logged_data_structure(model_path, basic_model):
    mlflow.sentence_transformers.save_model(model=basic_model, path=model_path)

    with model_path.joinpath("requirements.txt").open() as file:
        requirements = file.read()
    reqs = {req.split("==")[0] for req in requirements.split("\n")}
    expected_requirements = {"sentence-transformers", "torch", "transformers"}
    assert reqs.intersection(expected_requirements) == expected_requirements
    conda_env = yaml.safe_load(model_path.joinpath("conda.yaml").read_bytes())
    assert {req.split("==")[0] for req in conda_env["dependencies"][2]["pip"]}.intersection(
        expected_requirements
    ) == expected_requirements

    mlmodel = yaml.safe_load(model_path.joinpath("MLmodel").read_bytes())
    assert mlmodel["flavors"]["python_function"]["loader_module"] == "mlflow.sentence_transformers"
    assert (
        mlmodel["flavors"]["python_function"]["data"]
        == mlflow.sentence_transformers.SENTENCE_TRANSFORMERS_DATA_PATH
    )


def test_model_logging_and_inference(basic_model):
    artifact_path = "sentence_transformer"
    with mlflow.start_run():
        model_info = mlflow.sentence_transformers.log_model(
            model=basic_model, artifact_path=artifact_path
        )

    model = mlflow.sentence_transformers.load_model(model_info.model_uri)

    encoded_single = model.encode(
        "Encodings provide a fixed width output regardless of input size."
    )
    encoded_multi = model.encode(
        [
            "Just a small town girl",
            "livin in a lonely world",
            "she took the midnight train",
            "goin anywhere",
        ]
    )

    assert isinstance(encoded_single, np.ndarray)
    assert len(encoded_single) == 384
    assert isinstance(encoded_multi, np.ndarray)
    assert len(encoded_multi) == 4
    assert all(len(x) == 384 for x in encoded_multi)


def test_load_from_remote_uri(model_path, basic_model, mock_s3_bucket):
    mlflow.sentence_transformers.save_model(model=basic_model, path=model_path)
    artifact_root = f"s3://{mock_s3_bucket}"
    artifact_path = "model"
    artifact_repo = S3ArtifactRepository(artifact_root)
    artifact_repo.log_artifacts(model_path, artifact_path=artifact_path)
    model_uri = os.path.join(artifact_root, artifact_path)
    loaded = mlflow.sentence_transformers.load_model(model_uri=str(model_uri))

    encoding = loaded.encode(
        "I can see why these are useful when you do distance calculations on them!"
    )

    assert len(encoding) == 384


def test_log_model_calls_register_model(tmp_path, basic_model):
    artifact_path = "sentence_transformer"
    register_model_patch = mock.patch("mlflow.register_model")
    with mlflow.start_run(), register_model_patch:
        conda_env = tmp_path.joinpath("conda_env.yaml")
        _mlflow_conda_env(
            conda_env, additional_pip_deps=["transformers", "torch", "sentence-transformers"]
        )
        mlflow.sentence_transformers.log_model(
            model=basic_model,
            artifact_path=artifact_path,
            conda_env=str(conda_env),
            registered_model_name="My super cool encoder",
        )
        model_uri = f"runs:/{mlflow.active_run().info.run_id}/{artifact_path}"
        mlflow.register_model.assert_called_once_with(
            model_uri,
            "My super cool encoder",
            await_registration_for=DEFAULT_AWAIT_MAX_SLEEP_SECONDS,
        )


def test_log_model_with_no_registered_model_name(tmp_path, basic_model):
    artifact_path = "sentence_transformer"
    register_model_patch = mock.patch("mlflow.register_model")
    with mlflow.start_run(), register_model_patch:
        conda_env = tmp_path.joinpath("conda_env.yaml")
        _mlflow_conda_env(
            conda_env, additional_pip_deps=["transformers", "torch", "sentence-transformers"]
        )
        mlflow.sentence_transformers.log_model(
            model=basic_model,
            artifact_path=artifact_path,
            conda_env=str(conda_env),
        )
        mlflow.register_model.assert_not_called()


def test_log_with_pip_requirements(tmp_path, basic_model):
    expected_mlflow_version = _mlflow_major_version_string()

    requirements_file = tmp_path.joinpath("requirements.txt")
    requirements_file.write_text("some-clever-package")
    with mlflow.start_run():
        mlflow.sentence_transformers.log_model(
            basic_model, "model", pip_requirements=str(requirements_file)
        )
        _assert_pip_requirements(
            mlflow.get_artifact_uri("model"),
            [expected_mlflow_version, "some-clever-package"],
            strict=True,
        )
    with mlflow.start_run():
        mlflow.sentence_transformers.log_model(
            basic_model,
            "model",
            pip_requirements=[f"-r {requirements_file}", "a-hopefully-useful-package"],
        )
        _assert_pip_requirements(
            mlflow.get_artifact_uri("model"),
            [expected_mlflow_version, "some-clever-package", "a-hopefully-useful-package"],
            strict=True,
        )
    with mlflow.start_run():
        mlflow.sentence_transformers.log_model(
            basic_model,
            "model",
            pip_requirements=[f"-c {requirements_file}", "i-dunno-maybe-its-good"],
        )
        _assert_pip_requirements(
            mlflow.get_artifact_uri("model"),
            [expected_mlflow_version, "i-dunno-maybe-its-good", "-c constraints.txt"],
            ["some-clever-package"],
            strict=True,
        )


def test_log_with_extra_pip_requirements(basic_model, tmp_path):
    expected_mlflow_version = _mlflow_major_version_string()
    default_requirements = mlflow.sentence_transformers.get_default_pip_requirements()
    requirements_file = tmp_path.joinpath("requirements.txt")
    requirements_file.write_text("effective-package")
    with mlflow.start_run():
        mlflow.sentence_transformers.log_model(
            basic_model, "model", extra_pip_requirements=str(requirements_file)
        )
        _assert_pip_requirements(
            mlflow.get_artifact_uri("model"),
            [expected_mlflow_version, *default_requirements, "effective-package"],
        )
    with mlflow.start_run():
        mlflow.sentence_transformers.log_model(
            basic_model,
            "model",
            extra_pip_requirements=[f"-r {requirements_file}", "useful-package"],
        )
        _assert_pip_requirements(
            mlflow.get_artifact_uri("model"),
            [expected_mlflow_version, *default_requirements, "effective-package", "useful-package"],
        )
    with mlflow.start_run():
        mlflow.sentence_transformers.log_model(
            basic_model,
            "model",
            extra_pip_requirements=[f"-c {requirements_file}", "constrained-pkg"],
        )
        _assert_pip_requirements(
            mlflow.get_artifact_uri("model"),
            [
                expected_mlflow_version,
                *default_requirements,
                "constrained-pkg",
                "-c constraints.txt",
            ],
            ["effective-package"],
        )


def test_model_save_without_conda_env_uses_default_env_with_expected_dependencies(
    basic_model, model_path
):
    mlflow.sentence_transformers.save_model(basic_model, model_path)
    _assert_pip_requirements(
        model_path, mlflow.sentence_transformers.get_default_pip_requirements()
    )


def test_model_log_without_conda_env_uses_default_env_with_expected_dependencies(
    basic_model,
):
    artifact_path = "model"
    with mlflow.start_run():
        mlflow.sentence_transformers.log_model(basic_model, artifact_path)
        model_uri = mlflow.get_artifact_uri(artifact_path)
    _assert_pip_requirements(model_uri, mlflow.sentence_transformers.get_default_pip_requirements())


def test_log_model_with_code_paths(basic_model):
    artifact_path = "model"
    with mlflow.start_run(), mock.patch(
        "mlflow.sentence_transformers._add_code_from_conf_to_system_path"
    ) as add_mock:
        mlflow.sentence_transformers.log_model(basic_model, artifact_path, code_paths=[__file__])
        model_uri = mlflow.get_artifact_uri(artifact_path)
        _compare_logged_code_paths(__file__, model_uri, mlflow.sentence_transformers.FLAVOR_NAME)
        mlflow.sentence_transformers.load_model(model_uri)
        add_mock.assert_called()


def test_default_signature_assignment(model_path, basic_model):
    expected_signature = {
        "inputs": '[{"type": "string"}]',
        "outputs": '[{"type": "tensor", "tensor-spec": {"dtype": "float64", "shape": ' "[-1]}}]",
    }

    default_signature = mlflow.sentence_transformers._get_default_signature()

    assert default_signature.to_dict() == expected_signature


def test_model_pyfunc_save_load(basic_model, model_path):
    mlflow.sentence_transformers.save_model(basic_model, model_path)
    loaded_pyfunc = pyfunc.load_model(model_uri=model_path)

    sentence = "hello world and hello mlflow"
    sentences = [sentence, "goodbye my friends", "i am a sentence"]
    embedding_dim = basic_model.get_sentence_embedding_dimension()

    emb0 = loaded_pyfunc.predict(sentence)
    assert emb0.shape == (1, embedding_dim)

    emb1 = loaded_pyfunc.predict(sentences)
    emb2 = loaded_pyfunc.predict(pd.Series(sentences))
    emb3 = loaded_pyfunc.predict(pd.Series(sentences).to_numpy())

    for emb in [emb1, emb2, emb3]:
        assert emb.shape == (3, embedding_dim)

    np.testing.assert_array_equal(emb1, emb2)
    np.testing.assert_array_equal(emb1, emb3)


def test_spark_udf(basic_model, model_path, spark):
    with mlflow.start_run():
        model_info = mlflow.sentence_transformers.log_model(basic_model, "my_model")

    result_type = ArrayType(DoubleType())
    loaded_model = mlflow.pyfunc.spark_udf(
        spark,
        model_info.model_uri,
        result_type=result_type,
    )

    df = spark.createDataFrame([("hello MLflow",), ("bye world",)], ["text"])
    df = df.withColumn("embedding", loaded_model("text"))
    assert df.schema[1].dataType == result_type

    pdf = df.toPandas()
    assert pdf.shape == (2, 2)
    assert pdf["embedding"].dtype == "object"

    embeddings = np.array(pdf.embedding.to_list())
    assert embeddings.shape == (2, basic_model.get_sentence_embedding_dimension())


@pytest.mark.parametrize(
    "input1, input2",
    [
        (["hello world"], ["goodbye world!"]),
        (["hello world", "i am mlflow"], ["goodbye world!", "i am mlflow"]),
    ],
)
def test_pyfunc_serve_and_score(input1, input2, basic_model):
    with mlflow.start_run():
        model_info = mlflow.sentence_transformers.log_model(basic_model, "my_model")
    loaded_pyfunc = pyfunc.load_model(model_uri=model_info.model_uri)
    local_predict = loaded_pyfunc.predict(input1)

    # Check that the giving the same string to the served model results in the same result
    inference_data = json.dumps({"inputs": input1})
    resp = pyfunc_serve_and_score_model(
        model_info.model_uri,
        data=inference_data,
        content_type=pyfunc_scoring_server.CONTENT_TYPE_JSON,
        extra_args=["--env-manager", "local"],
    )
    serving_result = json.loads(resp.content.decode("utf-8"))["predictions"]
    np.testing.assert_array_equal(local_predict, serving_result)

    # Check that the giving a different string to the served model results in a different result
    inference_data = json.dumps({"inputs": input2})
    resp = pyfunc_serve_and_score_model(
        model_info.model_uri,
        data=inference_data,
        content_type=pyfunc_scoring_server.CONTENT_TYPE_JSON,
        extra_args=["--env-manager", "local"],
    )
    serving_result = json.loads(resp.content.decode("utf-8"))["predictions"]
    assert not np.equal(local_predict, serving_result).all()


SENTENCES = ["hello world", "i am mlflow"]
SIGNATURE = infer_signature(
    model_input=SENTENCES,
    model_output=SentenceTransformer("all-MiniLM-L6-v2").encode(SENTENCES),
)


@pytest.mark.parametrize(
    "example, signature",
    [
        (None, None),
        (SENTENCES, None),
        (None, SIGNATURE),
        (SENTENCES, SIGNATURE),
    ],
)
def test_signature_and_examples_are_saved_correctly(example, signature, basic_model, model_path):
    mlflow.sentence_transformers.save_model(
        basic_model, path=model_path, signature=signature, input_example=example
    )
    mlflow_model = Model.load(model_path)

    if signature is None:
        assert mlflow_model.signature == mlflow.sentence_transformers._get_default_signature()
    else:
        assert signature == mlflow_model.signature

    if example is None:
        assert mlflow_model.saved_input_example_info is None
    else:
        assert all((_read_example(mlflow_model, model_path) == example).all())
