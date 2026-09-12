#include "fragile/crypto.hpp"
#include <openssl/evp.h>
#include <openssl/rand.h>
#include <stdexcept>

namespace fragile {

std::vector<uint8_t> random_bytes(size_t n){
    std::vector<uint8_t> out(n);
    RAND_bytes(out.data(), (int)n);
    return out;
}

std::vector<uint8_t> derive_key_pbkdf2(const std::string& pwd, const std::vector<uint8_t>& salt, int iter){
    std::vector<uint8_t> key(32);
    PKCS5_PBKDF2_HMAC(pwd.c_str(), (int)pwd.size(), salt.data(), (int)salt.size(), iter, EVP_sha256(), (int)key.size(), key.data());
    return key;
}

std::vector<uint8_t> encrypt_aes_gcm(const std::vector<uint8_t>& pt, const std::vector<uint8_t>& key, const std::vector<uint8_t>& nonce){
    EVP_CIPHER_CTX* ctx=EVP_CIPHER_CTX_new();
    EVP_EncryptInit_ex(ctx, EVP_aes_256_gcm(), nullptr, nullptr, nullptr);
    EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_IVLEN, (int)nonce.size(), nullptr);
    EVP_EncryptInit_ex(ctx, nullptr, nullptr, key.data(), nonce.data());
    std::vector<uint8_t> out(pt.size()+16);
    int len=0, outlen=0;
    EVP_EncryptUpdate(ctx, out.data(), &len, pt.data(), (int)pt.size()); outlen=len;
    EVP_EncryptFinal_ex(ctx, out.data()+len, &len); outlen+=len;
    std::vector<uint8_t> tag(16); EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_GET_TAG, 16, tag.data());
    EVP_CIPHER_CTX_free(ctx);
    out.resize(outlen);
    out.insert(out.end(), tag.begin(), tag.end());
    return out;
}

std::vector<uint8_t> decrypt_aes_gcm(const std::vector<uint8_t>& ct, const std::vector<uint8_t>& key, const std::vector<uint8_t>& nonce){
    if(ct.size()<16) throw std::runtime_error("ciphertext too short");
    std::vector<uint8_t> tag(ct.end()-16, ct.end());
    std::vector<uint8_t> enc(ct.begin(), ct.end()-16);
    EVP_CIPHER_CTX* ctx=EVP_CIPHER_CTX_new();
    EVP_DecryptInit_ex(ctx, EVP_aes_256_gcm(), nullptr, nullptr, nullptr);
    EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_IVLEN, (int)nonce.size(), nullptr);
    EVP_DecryptInit_ex(ctx, nullptr, nullptr, key.data(), nonce.data());
    std::vector<uint8_t> out(enc.size());
    int len=0, outlen=0;
    EVP_DecryptUpdate(ctx, out.data(), &len, enc.data(), (int)enc.size()); outlen=len;
    EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_TAG, 16, tag.data());
    int ok=EVP_DecryptFinal_ex(ctx, out.data()+len, &len);
    EVP_CIPHER_CTX_free(ctx);
    if(!ok) throw std::runtime_error("decrypt failed");
    outlen+=len; out.resize(outlen);
    return out;
}

}
