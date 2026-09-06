/** 手机号登录/注册/绑定 API（验证码防爆破由后端承担） */
import { post } from './http'
import { type TokenPair } from './http'

export function sendPhoneCode(phone: string): Promise<{ message: string }> {
  return post('/api/phone/send-code', { phone })
}

export interface PhoneLoginResult extends TokenPair {
  username: string
  is_new: boolean
  default_password_hint: boolean
}

/** 验证码登录（自动注册） */
export function phoneLogin(phone: string, code: string): Promise<PhoneLoginResult> {
  return post('/api/phone/login', { phone, code })
}

export interface PhoneRegisterResult extends TokenPair {
  username: string
  is_new: boolean
  default_password_hint: boolean
}

/** 手机号注册（带密码与用户协议勾选） */
export function phoneRegister(phone: string, code: string, password: string, agree: boolean): Promise<PhoneRegisterResult> {
  return post('/api/phone/register', { phone, code, password, agree })
}

export interface BindPhoneResult extends TokenPair {
  username: string
  renamed: boolean
  message: string
}

/** 绑定手机号（后端可能改绑 username，必须换用返回的新 token 对） */
export function bindPhone(phone: string, code: string): Promise<BindPhoneResult> {
  return post('/api/user/bind-phone', { phone, code })
}
