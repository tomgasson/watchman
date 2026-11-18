/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */

#include "watchman/query/FileResult.h"
#include "watchman/ContentHash.h"
#include "watchman/fs/FileSystem.h"

namespace watchman {

FileResult::~FileResult() {}

std::optional<FileResult::ContentHash> FileResult::getContentGitSha1() {
  auto present = exists();
  auto info = stat();
  if (!present || !info) {
    return std::nullopt;
  }
  if (!*present) {
    throw std::system_error(
        std::make_error_code(std::errc::no_such_file_or_directory));
  }
  if (!info->isFile()) {
    throw std::system_error(std::make_error_code(std::errc::is_a_directory));
  }
  auto path = w_string::pathCat({dirName(), baseName()});
  auto hash = ContentHashCache::computeHashImmediate(
      path.c_str(), ContentHashKind::GitBlobSha1);
  auto after = getFileInformation(path.c_str());
  if (after.size != info->size || after.mtime.tv_sec != info->mtime.tv_sec ||
      after.mtime.tv_nsec != info->mtime.tv_nsec) {
    throw std::runtime_error(
        "metadata changed during Git blob hashing; query again");
  }
  return hash;
}

std::optional<DType> FileResult::dtype() {
  auto statInfo = stat();
  if (!statInfo.has_value()) {
    return std::nullopt;
  }
  return statInfo->dtype();
}

} // namespace watchman
