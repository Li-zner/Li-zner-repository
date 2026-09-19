/** 文件上传/查询 API（POST /v2/upload，GET /v2/files/{id}） */
import { upload } from './http'

export interface UploadedFile {
  file_id: string
  filename: string
  ext: string
  size: number
  uploaded_by: string
  text_length: number
  parse_note: string
}

export function uploadFile(file: File): Promise<UploadedFile> {
  return upload<UploadedFile>('/v2/upload', file)
}
