import ast
import json
import random
import kfp
from kfp import components
from kfp import dsl

# sagemaker_hpo_op = components.load_component_from_file(
#     "../../../../components/aws/sagemaker/hyperparameter_tuning/component.yaml"
#)
sagemaker_process_op = components.load_component_from_file(
    "../../../../components/aws/sagemaker/process/component.yaml"
)
sagemaker_train_op = components.load_component_from_file(
    "../../../../components/aws/sagemaker/TrainingJob/component.yaml"
)
sagemaker_model_op = components.load_component_from_file(
    "../../../../components/aws/sagemaker/model/component.yaml"
)
sagemaker_deploy_op = components.load_component_from_file(
    "../../../../components/aws/sagemaker/deploy/component.yaml"
)
sagemaker_batch_transform_op = components.load_component_from_file(
    "../../../../components/aws/sagemaker/batch_transform/component.yaml"
)


def processing_input(input_name, s3_uri, local_path):
    return {
        "InputName": input_name,
        "S3Input": {
            "S3Uri": s3_uri,
            "LocalPath": local_path,
            "S3DataType": "S3Prefix",
            "S3InputMode": "File",
        },
    }


def processing_output(output_name, s3_uri, local_path):
    return {
        "OutputName": output_name,
        "S3Output": {
            "S3Uri": s3_uri,
            "LocalPath": local_path,
            "S3UploadMode": "EndOfJob",
        },
    }


def training_input(input_name, s3_uri):
    return {
        "ChannelName": input_name,
        "DataSource": {"S3DataSource": {"S3Uri": s3_uri, "S3DataType": "S3Prefix"}},
    }

def json_encode_hyperparameters(hyperparameters):
    return {str(k): json.dumps(v) for (k, v) in hyperparameters.items()}



@dsl.pipeline(
    name="Image Classification Pipeline",
    description="Classify junction images",
)
def junction_classification(role_arn="", bucket_name=""):
    # Common component inputs
    region = "us-west-2"
    instance_type = "ml.m5.4xlarge"
    train_image = "xxxxxxxxxx.dkr.ecr.us-west-2.amazonaws.com/sagemaker-training-containers/script-mode-container-fastai:latest"
    inference_image="xxxxxxxxx.dkr.ecr.us-west-2.amazonaws.com/sagemaker-inference-containers/script-mode-container-fastai:latest"
    train_output_location = f"s3://{bucket_name}/junc-class/output"
    base_img="xxxxxxxxxx.dkr.ecr.us-west-2.amazonaws.com/kubeflow-containers/base-image:latest"



    trainingJobName = "sample-junc-classi-cond-v2-trainingjob" + str(random.randint(0, 999999))

    training = sagemaker_train_op(
        region=region,
        algorithm_specification={
            "trainingImage": train_image,
            "trainingInputMode": "File",
        },
        hyper_parameters=json_encode_hyperparameters({"lr": 1e-03}),
        input_data_config=[
            {
                "channelName": "train",
                "dataSource": {
                    "s3DataSource": {
                        "s3DataType": "S3Prefix",
                        "s3URI": f"s3://{bucket_name}/train/",
                        "s3DataDistributionType": "FullyReplicated"
                    }
                },
                "contentType": "application/x-image",
                "compressionType": "None"
            },
            {
                "channelName": "validation",
                "dataSource": {
                    "s3DataSource": {
                        "s3DataType": "S3Prefix",
                        "s3URI": f"s3://{bucket_name}/val/",
                        "s3DataDistributionType": "FullyReplicated"
                    }
                },
                "contentType": "application/x-image",
                "compressionType": "None"
            }
        ],

    output_data_config={"s3OutputPath": f"s3://{bucket_name}"},
        resource_config={
            "instanceCount": 1,
            "instanceType": "ml.m5.4xlarge",
            "volumeSizeInGB": 20,
        },
        role_arn=role_arn,
        training_job_name=trainingJobName,
        stopping_condition={"maxRuntimeInSeconds": 3600},
    )

    def get_s3_model_artifact(model_artifacts) -> str:
        import ast

        model_artifacts = ast.literal_eval(model_artifacts)
        return model_artifacts["s3ModelArtifacts"]

    get_s3_model_artifact_op = kfp.components.create_component_from_func(
        get_s3_model_artifact, output_component_file="get_s3_model_artifact.yaml"
    )
    model_artifact_url = get_s3_model_artifact_op(
        training.outputs["model_artifacts"]
    ).output

    create_model = sagemaker_model_op(
        region=region,
        model_name=trainingJobName + "-model",
        image=inference_image,
        model_artifact_url=model_artifact_url,
        role=role_arn,
    )


    batch_op=sagemaker_batch_transform_op(
        region=region,
        model_name=create_model.output,
        instance_type=instance_type,
        input_location=f"s3://{bucket_name}/test",
        content_type="application/x-image",
        output_location=f"s3://{bucket_name}/test_output",
    )

    print(batch_op.outputs['output_location'])

    def compute_f1(transform_output) -> float:
        import boto3
        import numpy as np 
        import json
        from sklearn.metrics import f1_score
        s3_client = boto3.client("s3")
        s3_resource = boto3.resource("s3")

        eval_bucket = s3_resource.Bucket(transform_output.split("/")[2])

        eval_prefix = "/".join(x for x in transform_output.split("/")[3:])

        labels = []
        preds = []
        for x in eval_bucket.objects.filter(Prefix=eval_prefix):
            labels.append(x.key.split("/")[-2])
            obj = s3_client.get_object(Bucket=eval_bucket.name, Key=x.key)
            j = json.loads(obj['Body'].read().decode('utf-8'))
            preds.append(j['predictions']['class'])

        labels = np.array(labels)
        preds = np.array(preds)

        f1score=f1_score(labels, preds, average='macro')
        return f1score

    get_f1_score_op = kfp.components.create_component_from_func(
        compute_f1,base_image=base_img,output_component_file="get_f1score.yaml")

    f1_score_val = get_f1_score_op(batch_op.outputs['output_location'] ).output

    with dsl.Condition(f1_score_val >0.5):
           sagemaker_deploy_op(
                region=region,
                model_name_1=create_model.output,
            )



        


if __name__ == "__main__":
    kfp.compiler.Compiler().compile(junc_classifier, __file__ + ".tar.gz")
    print("#####################Pipeline compiled########################")
