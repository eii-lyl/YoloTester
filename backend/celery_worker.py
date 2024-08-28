# celery_worker.py

import uuid
from celery import Celery
from celery.contrib.abortable import AbortableTask
from celery.signals import task_prerun, task_postrun, task_success, task_failure, task_revoked
import time
from pymongo import MongoClient
from bson.objectid import ObjectId
import os
from ultralytics import YOLO
from PIL import Image
import cv2
import io, json
from minio import Minio
import subprocess

# 创建MongoDB客户端
mongo_client = MongoClient('mongodb://mongo:27017/')
db = mongo_client['yolo_tasks']
task_collection = db['tasks']
media_collection = db['medias']
model_collection = db['models']

# 创建MinIO客户端
minio_client = Minio(
    "minio:9000",
    access_key=os.environ.get('MINIO_ACCESS_KEY'),
    secret_key=os.environ.get('MINIO_SECRET_KEY'),
    secure=False
)

# 创建Celery应用
app = Celery('yolo_tasks', broker='redis://redis:6379/0', backend='redis://redis:6379/0')

# 配置Celery
app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='Asia/Shanghai',
    enable_utc=True,
    worker_concurrency=4,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)

def update_collection(task_name, task_id, update_data):
    if task_name == 'convert_video':
        # 对于 convert_video 任务，我们需要通过 celery_task_id 查找对应的 video 记录
        video = media_collection.find_one({'celery_task_id': task_id})
        if video:
            media_collection.update_one(
                {'_id': video['_id']},
                {'$set': update_data}
            )
    elif task_name in ['run_yolo_image', 'run_yolo_video']:
        task_collection.update_one(
            {'celery_task_id': task_id},
            {'$set': update_data}
        )

@task_prerun.connect
def task_prerun_handler(task_id, task, *args, **kwargs):
    update_data = {'status': 'RUNNING', 'start_time': time.time()}
    update_collection(task.name, task_id, update_data)

@task_postrun.connect
def task_postrun_handler(task_id, task, *args, retval=None, state=None, **kwargs):
    if state:
        update_data = {'status': state, 'end_time': time.time()}
        update_collection(task.name, task_id, update_data)

@task_success.connect
def task_success_handler(sender, result, **kwargs):
    update_data = {'status': 'SUCCESS', 'result': result}
    update_collection(sender.name, sender.request.id, update_data)

@task_failure.connect
def task_failure_handler(sender, task_id, exception, einfo, *args, **kwargs):
    update_data = {'status': 'FAILURE', 'error_message': str(exception)}
    update_collection(sender.name, task_id, update_data)

@task_revoked.connect
def task_revoked_handler(request, terminated, signum, expired, **kwargs):
    update_data = {'status': 'REVOKED', 'end_time': time.time()}
    update_collection(request.task, request.id, update_data)

@app.task(base=AbortableTask, bind=True, name='convert_video')
def convert_video(self, video_id):
    try:
        video_info = media_collection.find_one({'_id': ObjectId(video_id)})
        if not video_info:
            raise Exception(f"Video with id {video_id} not found")

        minio_filename = video_info['minio_filename']
        file_extension = os.path.splitext(minio_filename)[1]
        unique_filename = f"{self.request.id}{file_extension}"
        local_filename = f"/tmp/{unique_filename}"

        # Download file from MinIO
        minio_client.fget_object("yolo-files", minio_filename, local_filename)

        # Get video metadata
        def get_video_metadata(file_path):
            result = subprocess.run([
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_format', '-show_streams', file_path
            ], capture_output=True, text=True)
            metadata = json.loads(result.stdout)

            video_stream = next((s for s in metadata['streams'] if s['codec_type'] == 'video'), None)

            return {
                'width': int(video_stream['width']),
                'height': int(video_stream['height']),
                'duration': float(metadata['format']['duration']),
                'vcodec': video_stream['codec_name'],
                'file_extension': os.path.splitext(file_path)[1],
            }

        # Extract metadata and update database
        metadata = get_video_metadata(local_filename)
        media_collection.update_one(
            {'_id': ObjectId(video_id)},
            {'$set': {
                'width': metadata['width'],
                'height': metadata['height'],
                'duration': metadata['duration'],
                'vcodec': metadata['vcodec'],
                'file_extension': metadata['file_extension'],
                'progress': 0
            }}
        )

        # Always convert to MP4
        print("Converting video to MP4...")
        converted_filename = f"/tmp/converted_{self.request.id}.mp4"
        ffmpeg_command = [
            '/usr/bin/ffmpeg', '-i', local_filename, 
            '-c:v', 'libx264', '-profile:v', 'high', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k',  # Add audio conversion
            '-movflags', '+faststart',  # Optimize for web streaming
            '-progress', 'pipe:1',
            '-y', converted_filename
        ]
        process = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)

        duration = metadata['duration']
        for line in process.stdout:
            if 'time=' in line:
                time_str = line.split('time=')[1].split()[0]
                hours, minutes, seconds = map(float, time_str.split(':'))
                current_time = hours * 3600 + minutes * 60 + seconds
                progress = int((current_time / duration) * 100)
                # Update progress in Video table
                media_collection.update_one(
                    {'_id': ObjectId(video_id)},
                    {'$set': {'progress': progress}}
                )

        process.wait()
        if process.returncode != 0:
            raise Exception("Video conversion failed")

        os.remove(local_filename)
        local_filename = converted_filename

        # Update metadata for converted video
        metadata = get_video_metadata(local_filename)

        # Upload converted file to MinIO
        converted_minio_filename = f"converted/converted_{video_id}.mp4"
        minio_client.fput_object("yolo-files", converted_minio_filename, local_filename)

        # Update Video table with file info and metadata
        media_collection.update_one(
            {'_id': ObjectId(video_id)},
            {
                '$set': {
                    'minio_filename': converted_minio_filename,
                    'progress': 100,
                    'width': metadata['width'],
                    'height': metadata['height'],
                    'duration': metadata['duration'],
                    'vcodec': metadata['vcodec'],
                    'file_extension': '.mp4',
                }
            }
        )

        # Clean up temporary file
        os.remove(local_filename)

        return {'status': 'success', 'converted_filename': converted_minio_filename}
    except Exception as e:
        # Update Video table with error status
        media_collection.update_one(
            {'_id': ObjectId(video_id)},
            {'$set': {'status': 'error', 'error_message': str(e)}}
        )
        raise e

@app.task(base=AbortableTask, bind=True, name='run_yolo_image')
def run_yolo_image(self, media_id, model_id, detect_class_indices, conf=0.25, imgsz=(1088, 1920), augment=False):
    print(f"开始处理照片 {media_id}")
    try:
        print(model_id)
        media_info = media_collection.find_one({'_id': ObjectId(media_id)})
        if not media_info:
            raise Exception(f"File with id {media_id} not found")

        # 从MinIO下载文件
        minio_filename = media_info['minio_filename']
        file_extension = os.path.splitext(minio_filename)[1]
        unique_filename = f"{self.request.id}{file_extension}"
        local_filename = f"/tmp/{unique_filename}"
        minio_client.fget_object("yolo-files", minio_filename, local_filename)

        # 加载YOLO模型
        model_info = model_collection.find_one({'_id': ObjectId(model_id)})
        model = YOLO(model_info['model_path'])

        # 进行预测
        results = model.predict(local_filename, conf=conf, imgsz=imgsz, augment=augment, classes=detect_class_indices)

        # 处理结果
        im_array = results[0].plot(font_size=8, line_width=1)
        im = Image.fromarray(im_array[..., ::-1])  # RGB PIL image

        # 保存结果图像       
        result_filename = f"result_{unique_filename}"
        result_filename_relative_path = f"results/{result_filename}"
        os.makedirs(os.path.dirname(result_filename_relative_path), exist_ok=True)
        im.save(result_filename_relative_path)

        # 将结果上传到MinIO
        minio_client.fput_object("yolo-files", f"results/{result_filename}", result_filename_relative_path)

        # 更新任务状态
        task_collection.update_one(
            {'celery_task_id': self.request.id},
            {'$set': {'progress': 100, 'result_file': f"results/{result_filename}"}}
        )

        # 清理临时文件
        os.remove(local_filename)
        os.remove(result_filename_relative_path)

        print(f"照片 {media_id} 处理完成")
        return f"照片 {media_id} 处理完成，结果文件：results/{result_filename}"
    except Exception as e:
        print(f"照片 {media_id} 处理出错: {str(e)}")
        raise

@app.task(base=AbortableTask, bind=True, name='run_yolo_video')
def run_yolo_video(self, media_id, model_id, detect_class_indices, conf=0.25, imgsz=(1088, 1920), augment=False):
    print(f"开始处理视频 {media_id}")
    try:
        media_info = media_collection.find_one({'_id': ObjectId(media_id)})
        if not media_info:
            raise Exception(f"File with id {media_id} not found")

        # 从MinIO下载文件
        minio_filename = media_info['minio_filename']
        file_extension = os.path.splitext(minio_filename)[1]
        unique_filename = f"{self.request.id}{file_extension}"
        local_filename = f"/tmp/{unique_filename}"
        minio_client.fget_object("yolo-files", minio_filename, local_filename)

        # 加载YOLO模型
        model_info = model_collection.find_one({'_id': ObjectId(model_id)})
        model = YOLO(model_info['model_path'])

        # 设置视频写入器
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        output_filename = f"result_{unique_filename.split('.')[0]}.mp4"
        output_filename_relative_path = f"results/{output_filename}"
        os.makedirs(os.path.dirname(output_filename_relative_path), exist_ok=True)
        fps = cv2.VideoCapture(local_filename).get(cv2.CAP_PROP_FPS)
        out = cv2.VideoWriter(output_filename_relative_path, fourcc, fps, (imgsz[1], imgsz[0]))

        # 进行预测
        results = model(local_filename, stream=True, conf=conf, imgsz=imgsz, augment=augment, classes=detect_class_indices)

        frame_count = 0
        total_frames = int(cv2.VideoCapture(local_filename).get(cv2.CAP_PROP_FRAME_COUNT))

        for result in results:
            frame_count += 1
            progress = int((frame_count / total_frames) * 100)

            # 处理每一帧
            im_array = result.plot()
            frame = cv2.resize(im_array, (imgsz[1], imgsz[0]))
            out.write(frame)

            # 更新进度
            task_collection.update_one(
                {'celery_task_id': self.request.id},
                {'$set': {'progress': progress}}
            )

            if self.is_aborted():
                print(f"视频 {media_id} 处理被中止")
                out.release()
                return "Task aborted"

        out.release()

        # FFmpeg转码
        transcoded_output = f"results/transcoded_{output_filename}"
        ffmpeg_command = [
            '/usr/bin/ffmpeg', '-i', output_filename_relative_path, 
            '-c:v', 'libx264', '-profile:v', 'high', '-pix_fmt', 'yuv420p',
            '-y', transcoded_output
        ]
        subprocess.run(ffmpeg_command, check=True)

        # 将结果上传到MinIO
        minio_client.fput_object("yolo-files", f"results/{output_filename}", transcoded_output)

        # 更新任务状态
        task_collection.update_one(
            {'celery_task_id': self.request.id},
            {'$set': {'progress': 100, 'result_file': f"results/{output_filename}"}}
        )

        # 清理临时文件
        os.remove(local_filename)
        os.remove(output_filename_relative_path)
        os.remove(transcoded_output)

        print(f"视频 {media_id} 处理完成")
        return f"视频 {media_id} 处理完成，结果文件：results/{output_filename}"
    except Exception as e:
        print(f"视频 {media_id} 处理出错: {str(e)}")
        raise

if __name__ == '__main__':
    app.worker_main(["worker", "--loglevel=info"])